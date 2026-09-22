# -*- coding: utf-8 -*-
"""전체 workflow(Stage1→Stage2 Track1/Track2) 실행 + 인수인계 계약 경로 영속화.

`task_episode/run_workflow_audit.py`(2026-09-08 최초 러너, 카운트 요약만 저장) 승격판.
pipeline-integrator 담당: `docs/design/AGENT_DESIGN.md` §3 인수인계 표의 경로에 실제로
산출물을 쓴다. 판정 로직은 그대로(`tag_v08.tag_clip_v08`·`candidates.generate_candidates`
·`retrieve.index_clip`·`events.detect_events`) — 여기서는 그 반환값을 계약 경로로 배선만
한다.

쓰는 파일 (2026-09-11: exp 네임스페이스 분리 — 재실행마다 이전 사이클 클립별 산출물이
덮어써지던 문제 수정. `decisions/DESIGN_LOG.md` [2026-09-11] 참고):
  outputs/episodes/<exp>/<clip>.json           S0+S1 — 에피소드 구간 + 후보 집합 전량(미선택 포함)
  outputs/labels/<exp>/<clip>/s2.json          S2 raw(자유 서술, tag_v08._reason 출력)
  outputs/labels/<exp>/<clip>/s3.json          S3 raw(GT override 이전 구조화 스냅샷)
  outputs/labels/<exp>/<clip>/final.json       최종 결정(Track1 rec 전량 + Track2 index)
  experiments/cards/<exp>.md              실행 전 자동 등록 카드(사전 등록 요건)
  experiments/results/<exp>/report.json   기존 감사 요약(성공/실패·CJK·파싱실패·arc 불일치)
  experiments/results/<exp>/manifest.json 재현성 매니페스트 5군(common/manifest.py)

2026-09-11(추가): s0["map_valid"](클립 단위)를 `flags.fallback_path`로
outputs/episodes/<exp>/<clip>.json의 s1_candidate_episodes[]와
outputs/labels/<exp>/<clip>/final.json의 track1[] 각 원소에 복사(무기록 fallback 금지,
CLAUDE.md §3). 값은 클립 전체 단위 그대로 — 에피소드별 세분화 아님(map_valid 자체의 한계).

실행: ./run.sh task_episode/run_pipeline.py --set gold50 --exp C001 [--n N] [--workers 4]
"""
import argparse
import json
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import paths as P
from dataset import clip_duration
from client import build_client_pool
import events
import map_lane as M
from classify073 import consolidate_episodes
import tag_v08 as V08
import candidates as CA
import retrieve as R
import manifest as MF

ROOT = Path("/home/daejun/vla-tagging")
CLIP_SETS = {
    "gold50": ROOT / "gold_label" / "sample_clips.json",          # gold.json 인간 라벨 보유 50 — 채점 가능
    "selected50": ROOT / "gold_label" / "select" / "selected50.json",  # Stage1 top-50 — 회귀 기준선(gold 교집합 0)
    "vis100": ROOT / "gold_label" / "vis100.json",  # 2026-09 재구성 신규 100 clip(nvidia/<clip>/, gold 라벨 없음 — 채점 불가, S0/스키마 검증용)
}
CJK_RE = re.compile(r"[぀-ヿ一-鿿]")


def cjk_flag(text):
    return bool(CJK_RE.search(text or ""))


def _count_cjk(rec):
    fields = [rec.get("scene_description"), rec.get("ego_intent"), rec.get("chain_of_causation")]
    for c in rec.get("critical_components") or []:
        fields.append(c.get("description")); fields.append(c.get("why_critical"))
    return sum(1 for f in fields if cjk_flag(f))


def _write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def audit_clip(client, pool, clip_id, exp):
    rec = {"clip_id": clip_id}
    path = P.video_path(clip_id)

    # --- S0: 전이검출(production 배선 그대로 — tag_v08.py:222-223과 동일) ---
    t0 = time.time()
    try:
        # 2026-09-11: video_meta(path) → clip_duration(clip_id, path) — 신규 visionary-nvidia
        # 100 clip(mp4 없음, index.parquet 프레임 갤러리)은 P.video_path()가 존재하지 않는
        # 경로를 반환하므로 소스 자동판별 창구로 교체(vlm-engineer, dataset.clip_duration()).
        # 판정 로직(events.detect_events 등) 불변 — dur 스칼라 입력 경로만 바뀜.
        dur = clip_duration(clip_id, path)["duration_s"]
        curvature_fn = M.default_curvature_fn(clip_id, dur)
        lane_crossing_fn = M.default_lane_crossing_fn(clip_id, dur)
        det = events.detect_events(clip_id, curvature_fn=curvature_fn, lane_crossing_fn=lane_crossing_fn)
        s0 = {"ok": bool(det.get("ok")), "dur": dur, "map_valid": curvature_fn is not None,
              "n_raw_events": len(det.get("events", []))}
        eps = consolidate_episodes(det["events"]) if det.get("ok") else []
        s0["n_episodes"] = len(eps)
        s0["episode_kinds"] = [ep["kinds"] for ep in eps]
        if not det.get("ok"):
            s0["reason"] = det.get("reason")
    except Exception as e:
        s0 = {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-400:]}
    s0["elapsed_s"] = round(time.time() - t0, 1)
    rec["s0"] = s0

    # --- Track1: tag_v08 (S1+S2+S3) ---
    t0 = time.time()
    try:
        t1 = V08.tag_clip_v08(client, path, clip_id)
    except Exception as e:
        t1 = {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-400:]}
    t1_summary = {"ok": bool(t1.get("ok")), "elapsed_s": round(time.time() - t0, 1), "error": t1.get("error")}
    recs = t1.get("records", []) if t1.get("ok") else []
    if t1.get("ok"):
        t1_summary["n_records"] = len(recs)
        t1_summary["n_parse_fail"] = sum(1 for r in recs if r.get("scene_description") is None)
        t1_summary["n_cjk"] = sum(_count_cjk(r) for r in recs)
        t1_summary["arc_by_segment"] = [r["ego_context"]["arc"] for r in recs]
        t1_summary["cause_by_segment"] = [r["cause"] for r in recs]
    rec["track1"] = t1_summary

    # --- Track2: candidates -> retrieve (S1+S2) ---
    t0 = time.time()
    cand = None
    try:
        cand = CA.generate_candidates(pool, path, clip_id)
        idx = R.index_clip(cand) if cand.get("ok") else None
    except Exception as e:
        cand = {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-400:]}
        idx = None
    t2_summary = {"ok": bool(cand.get("ok")), "elapsed_s": round(time.time() - t0, 1), "error": cand.get("error")}
    if cand.get("ok"):
        t2_summary["n_episodes"] = len(cand.get("episodes", []))
        t2_summary["arc_by_segment"] = [ep.get("arc", []) for ep in cand.get("episodes", [])]
        if idx:
            t2_summary["n_tags_total"] = sum(len(e["tags"]) for e in idx["episodes"])
    rec["track2"] = t2_summary

    # --- Track1 vs Track2 arc 불일치 — 곡률보정 배선 차이 잔존 확인용(1브랜치1축, 이번 범위 밖) ---
    if t1_summary["ok"] and t2_summary["ok"]:
        a1 = sorted(set(k for arc in t1_summary["arc_by_segment"] for k in arc))
        a2 = sorted(set(k for arc in t2_summary["arc_by_segment"] for k in arc))
        rec["track_arc_mismatch"] = (a1 != a2)
        rec["track1_arc_union"] = a1
        rec["track2_arc_union"] = a2

    # ============ 계약 경로 영속화 (pipeline-integrator 신설분, 2026-09-09 / 2026-09-11 exp 분리) ============
    # fallback_path 배선(2026-09-11): s0["map_valid"](76-78행, 클립 전체 단위 계산)를
    # map_lane.py 독스트링의 corridor 근사(Branch B) 사용 여부 플래그로 하위 산출물에 복사한다
    # (CLAUDE.md 강제규칙4 "무기록 fallback 금지"). map_valid 자체가 클립 단위 값이라 에피소드별로
    # 다르게 낼 수 없다는 한계는 그대로 둔다 — 판정 로직은 건드리지 않고 값만 옮겨 적는다.
    # s0 계산이 예외로 실패해 map_valid 키가 없는 경우는 안전측(fallback_path=True)으로 기록한다.
    fallback_path = not bool(s0.get("map_valid", False))

    # outputs/episodes/<exp>/<clip>.json — S0+S1: 에피소드 구간 + 후보 집합 전량(미선택 포함)
    s1_eps = cand.get("episodes", []) if cand and cand.get("ok") else []
    for ep in s1_eps:
        ep["flags"] = {**(ep.get("flags") or {}), "fallback_path": fallback_path}
    episodes_doc = {
        "clip_id": clip_id, "dur": s0.get("dur"),
        "s0": {k: v for k, v in s0.items() if k not in ("trace",)},
        # candidates.generate_candidates()의 episodes[].candidates 는 3채널 OR 합집합이라
        # 선택되지 않은 후보(단일채널·낮은 vote)까지 그대로 보존한다 — 누락오류/선택오류 분리 근거.
        "s1_candidate_episodes": s1_eps,
        "s1_ok": bool(cand.get("ok")) if cand else False,
    }
    _write_json(ROOT / "outputs" / "episodes" / exp / f"{clip_id}.json", episodes_doc)

    # outputs/labels/<exp>/<clip>/{s2,s3,final}.json — S2/S3 분리 관찰(tag_v08._s2_raw/_s3_raw 캡처값)
    s2_doc = [{"segment_id": r.get("segment_id"), "key_frame_t": r.get("key_frame_t"),
               "think": r.get("_s2_raw")} for r in recs]
    s3_doc = [{"segment_id": r.get("segment_id"), "raw": r.get("_s3_raw")} for r in recs]
    final_recs = []
    for r in recs:
        r2 = {k: v for k, v in r.items() if k not in ("_s2_raw", "_s3_raw")}
        r2["flags"] = {**(r2.get("flags") or {}), "fallback_path": fallback_path}
        final_recs.append(r2)
    final_doc = {"clip_id": clip_id, "track1": final_recs,
                 "track2": idx if (cand and cand.get("ok") and idx) else None}
    _write_json(ROOT / "outputs" / "labels" / exp / clip_id / "s2.json", s2_doc)
    _write_json(ROOT / "outputs" / "labels" / exp / clip_id / "s3.json", s3_doc)
    _write_json(ROOT / "outputs" / "labels" / exp / clip_id / "final.json", final_doc)

    return rec


def _ensure_card(exp: str, clip_set: str, n: int):
    """실행 전 자동 카드 등록(사전 등록 요건 형식 충족) — check_experiment_card.py 파싱 요건 그대로."""
    card = ROOT / "experiments" / "cards" / f"{exp}.md"
    if card.exists():
        return card
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text(
        f"# 실험 카드 {exp}\n\n"
        f"가설: 에이전트 자율 루프 배선(계약 경로 실체화 + pipeline-integrator 신설)이 "
        f"S0~S3 산출물을 관찰 가능하게 만든다.\n"
        f"변경 축: 루프 배선 도입\n"
        f"대조 조건: 배선 도입 전(계약 경로 부재, `run_workflow_audit.py` 요약만) 대비 "
        f"동일 clip 세트({clip_set}, n={n})\n"
        f"등록 시각: {now}\n\n"
        f"반증 조건: 계약 경로 6곳 중 하나라도 산출물이 생성되지 않거나, "
        f"pre-commit 훅이 여전히 '대상 없음'으로 침묵 통과하면 이 카드는 기각.\n"
        f"주의: 이 사이클은 배선 도입과 실행이 섞인 기준선(baseline)이다 — 통제 실험이 아니며 "
        f"효과 귀속에 쓰지 않는다.\n",
        encoding="utf-8")
    return card


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=list(CLIP_SETS), default="gold50",
                     help="gold50=gold.json 인간라벨 50(채점 가능) / selected50=Stage1 top-50(회귀 기준선)")
    ap.add_argument("--selected", default=None, help="--set 대신 직접 clip_id 리스트 JSON 경로 지정")
    ap.add_argument("--exp", default=None, help="experiments/{cards,results}/<exp> 식별자, 예: C001")
    ap.add_argument("--n", type=int, default=None)
    ap.add_argument("--workers", type=int, default=4,
                     help="clip 단위 동시 실행 수(clip 간 의존성 없음)")
    args = ap.parse_args()

    src = Path(args.selected) if args.selected else CLIP_SETS[args.set]
    clip_ids = json.loads(src.read_text())
    if args.n:
        clip_ids = clip_ids[:args.n]
    exp = args.exp or f"C_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    print(f"대상 clip {len(clip_ids)}개 (source={src}, set={args.set}, exp={exp})", flush=True)

    card = _ensure_card(exp, args.set, len(clip_ids))
    print(f"실험 카드 등록: {card}", flush=True)

    pool = build_client_pool()
    print(f"replica {len(pool)}개 가동 · workers={args.workers}", flush=True)

    results_by_id = {}
    t_wall0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(audit_clip, pool[i % len(pool)], pool, cid, exp): cid
                for i, cid in enumerate(clip_ids)}
        done = 0
        for fut in as_completed(futs):
            cid = futs[fut]
            done += 1
            try:
                results_by_id[cid] = fut.result()
            except Exception as e:
                results_by_id[cid] = {
                    "clip_id": cid,
                    "s0": {"ok": False, "error": f"{type(e).__name__}: {e}"},
                    "track1": {"ok": False, "error": f"{type(e).__name__}: {e}"},
                    "track2": {"ok": False, "error": f"{type(e).__name__}: {e}"},
                }
            print(f"[{done}/{len(clip_ids)}] {cid[:8]} 완료", flush=True)
    wall_s = round(time.time() - t_wall0, 1)
    results = [results_by_id[cid] for cid in clip_ids]  # 원래 순서로 복원

    out_dir = ROOT / "experiments" / "results" / exp
    _write_json(out_dir / "report.json", results)

    # frame_rate·resolution 미지정 — MF.build()가 config.SEND_FPS·config.WINDOW_MAX_SIDE를
    # 기본값으로 채움(common/manifest.py:32-33). 여기서 리터럴로 재대입하지 않는다.
    man = MF.build(seed=None,
                    gt_version="visionary-nvidia", vocab_version="vocab073+taxonomy(v0.4 미전환)",
                    threshold_set_id="unversioned-2026-09", replicas=[str(c.base_url) for c in pool])
    man["run"] = {"exp": exp, "clip_set": args.set, "n_clips": len(clip_ids),
                   "workers": args.workers, "wall_clock_s": wall_s,
                   "report_hash": MF.content_hash(results)}
    _write_json(out_dir / "manifest.json", man)

    n = len(results)
    s0_ok = sum(1 for r in results if r["s0"].get("ok"))
    t1_ok = sum(1 for r in results if r["track1"].get("ok"))
    t2_ok = sum(1 for r in results if r["track2"].get("ok"))
    n_cjk = sum(r["track1"].get("n_cjk", 0) for r in results)
    n_parse_fail = sum(r["track1"].get("n_parse_fail", 0) for r in results)
    n_mismatch = sum(1 for r in results if r.get("track_arc_mismatch"))
    n_map_valid = sum(1 for r in results if r["s0"].get("map_valid"))
    both_ok = sum(1 for r in results if r["track1"].get("ok") and r["track2"].get("ok"))
    print(f"\n=== 요약 ({n} clip, exp={exp}) ===")
    print(f"S0 성공 {s0_ok}/{n} · Track1 성공 {t1_ok}/{n} · Track2 성공 {t2_ok}/{n}")
    print(f"map_valid {n_map_valid}/{n} · CJK혼입 {n_cjk}건 · guided_json 파싱실패 {n_parse_fail}건")
    print(f"Track1/Track2 arc 불일치 {n_mismatch}/{both_ok}(둘 다 성공한 clip 기준)")
    print(f"wall-clock {wall_s}s = {wall_s/60:.1f}분 (workers={args.workers})")
    print(f"결과 저장 -> {out_dir/'report.json'} (+ manifest.json)")
    print(f"계약 경로: outputs/episodes/{exp}/<clip>.json · outputs/labels/{exp}/<clip>/{{s2,s3,final}}.json")


if __name__ == "__main__":
    main()
