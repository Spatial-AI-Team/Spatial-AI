# -*- coding: utf-8 -*-
"""verifier 최종 리포트 자동 생성 — `reports/<exp>.md`.

계약 경로별 산출물 존재 + 꼬임 체크리스트 6항(합의된 계획서 W6) + 목표 완수도(현재 갭)를
수치로 채운다. 판정은 하지 않는다 — 있는 사실과 없는 사실을 나열한다.

실행: ./run.sh verification/report.py --exp C001   (gate.py·score_gold.py 실행 후)
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "common"))
import disclosure

# tag_vocab_v0.4.json 대비 현재 track1(final.json) 레코드에 실재하는지 확인할 필드.
# 이미 미구현으로 문서화된 항목(CLAUDE.md §4, DESIGN_LOG) — 여기서 수치로 재확인만 한다.
_V04_FIELD_CHECKS = {
    "transition_filters_passed": lambda r: "transition_filters_passed" in r,
    "curvature_correction_source": lambda r: "curvature_correction_source" in r,
    "merged_from": lambda r: "merged_from" in r,
    "window_start_reason": lambda r: "window_start_reason" in r,
    "window_end_reason": lambda r: "window_end_reason" in r,
    "threshold_set_id": lambda r: "threshold_set_id" in r,
    "physical_gate_passed": lambda r: "physical_gate_passed" in r,
    "camera_visible": lambda r: "camera_visible" in r,
    "episode_confidence": lambda r: "episode_confidence" in r,
}


def _load(exp):
    base = ROOT / "experiments" / "results" / exp
    def j(name):
        p = base / name
        return json.loads(p.read_text()) if p.exists() else None
    return base, j("report.json"), j("manifest.json"), j("gate.json"), j("gold_score_summary.json")


def _contract_table(clip_ids, base, gate, gold_summary, exp):
    rows = []
    n = len(clip_ids)
    n_ep = sum(1 for c in clip_ids if (ROOT / "outputs" / "episodes" / exp / f"{c}.json").exists())
    n_s2 = sum(1 for c in clip_ids if (ROOT / "outputs" / "labels" / exp / c / "s2.json").exists())
    n_s3 = sum(1 for c in clip_ids if (ROOT / "outputs" / "labels" / exp / c / "s3.json").exists())
    n_final = sum(1 for c in clip_ids if (ROOT / "outputs" / "labels" / exp / c / "final.json").exists())
    rows.append(("outputs/episodes/<exp>/<clip>.json (S0+S1, rule-engineer)", f"{n_ep}/{n}"))
    rows.append(("outputs/labels/<exp>/<clip>/s2.json (S2 raw, vlm-engineer)", f"{n_s2}/{n}"))
    rows.append(("outputs/labels/<exp>/<clip>/s3.json (S3 raw, vlm-engineer)", f"{n_s3}/{n}"))
    rows.append(("outputs/labels/<exp>/<clip>/final.json (배선, pipeline-integrator)", f"{n_final}/{n}"))
    rows.append(("experiments/results/<exp>/manifest.json (experiment-runner)", "생성됨" if (base/"manifest.json").exists() else "없음"))
    rows.append(("experiments/results/<exp>/gate.json (verifier)", "생성됨" if gate is not None else "없음"))
    rows.append(("experiments/results/<exp>/gold_score_summary.json (verifier)", "생성됨" if gold_summary is not None else "없음"))
    return rows


def _anchor_contamination_check():
    """앵커 비오염(원칙 1) 정적 확인 — events.py가 VLM 클라이언트를 참조하는지 grep."""
    src = (ROOT / "common" / "events.py").read_text(encoding="utf-8")
    bad = re.search(r"\bclient\b|build_client_pool|chat\.completions", src)
    return {"file": "common/events.py", "vlm_reference_found": bool(bad),
            "verdict": "위반" if bad else "위반 없음(모델 유래 값이 앵커 경로에 없음)"}


def _fallback_unlogged(results):
    n_map_invalid = sum(1 for r in results if r["s0"].get("ok") and not r["s0"].get("map_valid"))
    return {"n_map_invalid_clips": n_map_invalid,
            "fallback_path_field_recorded": False,
            "finding": f"{n_map_invalid}개 clip이 map_valid=false(코리도어 근사 경로로 추정)로 실행됐으나 "
                       "`fallback_path` 플래그가 어떤 산출물에도 기록되지 않는다 — 무기록 fallback 금지 원칙 위반(기존 갭, 이번 사이클서 미수정)"}


def _s2_conditioning_mismatch():
    return {"schema_default": "minimal", "measured": disclosure.S2_CONDITIONING,
            "mismatch": disclosure.S2_CONDITIONING != "minimal",
            "note": "candidates/vlm_verify/tag_v08 프롬프트가 GT 카테고리 힌트를 상시 주입 — "
                    "CLAUDE.md §3 전환 규칙(실험 없이 기존 경로 안 바꿈)에 따라 코드는 그대로, 사실만 기록"}


def _completion_coverage(clip_ids, exp):
    n_total = 0
    hits = {k: 0 for k in _V04_FIELD_CHECKS}
    for cid in clip_ids:
        fp = ROOT / "outputs" / "labels" / exp / cid / "final.json"
        if not fp.exists():
            continue
        recs = (json.loads(fp.read_text()).get("track1") or [])
        for r in recs:
            n_total += 1
            for k, fn in _V04_FIELD_CHECKS.items():
                if fn(r):
                    hits[k] += 1
    return n_total, hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", required=True)
    args = ap.parse_args()

    base, results, manifest, gate, gold_summary = _load(args.exp)
    if results is None:
        print(f"experiments/results/{args.exp}/report.json 없음 — 먼저 run_pipeline.py 실행", file=sys.stderr)
        sys.exit(1)
    clip_ids = [r["clip_id"] for r in results]
    n = len(clip_ids)

    contract_rows = _contract_table(clip_ids, base, gate, gold_summary, args.exp)
    anchor = _anchor_contamination_check()
    mismatch_n = sum(1 for r in results if r.get("track_arc_mismatch"))
    both_ok = sum(1 for r in results if r["track1"].get("ok") and r["track2"].get("ok"))
    fallback = _fallback_unlogged(results)
    s2c = _s2_conditioning_mismatch()
    n_total_recs, hits = _completion_coverage(clip_ids, args.exp)

    lines = []
    lines.append(f"# 사이클 리포트 {args.exp}\n")
    lines.append(f"clip {n}개 · wall-clock {manifest['run']['wall_clock_s']}s" if manifest else f"clip {n}개")
    lines.append("\n**주의**: 이 리포트는 게이트 통과율·완수도 수치를 정확도로 보고하지 않는다(원칙 5). "
                  "게이트 산출은 `fail`/`undetermined` 두 값뿐이며 `pass`는 없다.\n")

    lines.append("## 1. 역할대로 동작 — 계약 경로별 산출물\n")
    lines.append("| 경로(담당 에이전트) | 현황 |\n|---|---|")
    for name, val in contract_rows:
        lines.append(f"| {name} | {val} |")

    lines.append("\n## 2. 앵커 오염 확인 (원칙 1)\n")
    lines.append(f"- `{anchor['file']}` VLM 참조: {'발견됨' if anchor['vlm_reference_found'] else '없음'} → **{anchor['verdict']}**")

    lines.append("\n## 3. Track1/Track2 arc 불일치\n")
    lines.append(f"- 둘 다 성공한 clip 중 arc union 불일치: {mismatch_n}/{both_ok}")
    lines.append("- 잔존 원인: `classify073.py`·`taxo_detect.py`·`vlm_verify.py`·`map_lane.py`·`selection.py`·"
                  "`folder_selection.py`의 무보정 호출부 6곳(이번 사이클 범위 밖, 1브랜치1축)")

    lines.append("\n## 4. 무기록 fallback\n")
    lines.append(f"- map_valid=false clip: {fallback['n_map_invalid_clips']}/{n}")
    lines.append(f"- {fallback['finding']}")

    lines.append("\n## 5. 스키마-실측 불일치\n")
    lines.append(f"- `flags.s2_conditioning` 스키마 기본값 `{s2c['schema_default']}` vs 실측 `{s2c['measured']}` "
                  f"→ **{'불일치' if s2c['mismatch'] else '일치'}**")
    lines.append(f"- {s2c['note']}")

    lines.append("\n## 6. 목표 완수도 (tag_vocab_v0.4.json 대비, 미달 예상됨)\n")
    lines.append("- `cause` 값 집합: 코드(`task_episode/tag_v08.py` `_CAUSE_ENUM`) 4값"
                 "(agent/signal/road_geometry/other) vs 목표 6값"
                 "(agent/static_object/traffic_control/road_geometry/road_condition/ego_intent)"
                 " — **정적 사실, per-record 계산 아님**(개별 값이 우연히 겹쳐 계산하면 왜곡됨)")
    lines.append(f"\ntrack1 레코드 {n_total_recs}건 기준 필드 커버리지:\n")
    lines.append("| 필드 | 존재 | 커버리지 |\n|---|---|---|")
    for k, cnt in hits.items():
        frac = f"{cnt}/{n_total_recs}" if n_total_recs else "n/a"
        lines.append(f"| {k} | {cnt} | {frac} |")
    lines.append("\n격차는 이번 사이클에서 메우지 않는다 — 다음 사이클 변경축 우선순위 근거로 남긴다.")

    if gate:
        n_segs = sum(len(c["checks"]) for c in gate)
        n_fail = sum(1 for c in gate for f in c["checks"] if f["any_fail"])
        lines.append("\n## 7. 물리 게이트 (준거 B)\n")
        lines.append(f"- segment {n_segs}개 중 any_fail {n_fail}개 (나머지는 undetermined — 대부분 필드 미구현으로 인한 정상 산출)")

    if gold_summary:
        lines.append("\n## 8. gold 50 채점 (준거 A)\n")
        lines.append(f"- recall = {gold_summary['recall']} (n_human_tags={gold_summary['n_human_tags']})")
        lines.append(f"- precision_lower_bound = {gold_summary['precision_lower_bound']} — **{gold_summary['precision_caveat']}**")
        lines.append(f"- anchor_miss episodes = {gold_summary['n_anchor_miss_episodes']}, S1 실패 clip = {gold_summary['n_clips_s1_failed']}")
        lines.append(f"- 오류코드별: {gold_summary['error_by_code']}")
        lines.append(f"- {gold_summary['err_attrib_note']}")

    lines.append("\n## 9. 전 사이클 회귀\n")
    prev = sorted((ROOT / "experiments" / "results").glob("C*/gold_score_summary.json"))
    prev = [p for p in prev if p.parent.name != args.exp]
    if not prev:
        lines.append("- 이전 사이클 없음 — 이 사이클은 기준선(baseline)이며 통제 실험이 아니다(효과 귀속에 쓰지 않는다)")
    else:
        lines.append(f"- 비교 대상 {len(prev)}건 존재 — 상세 diff는 별도 실행 필요")

    out_path = ROOT / "reports" / f"{args.exp}.md"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"저장 -> {out_path}")


if __name__ == "__main__":
    main()
