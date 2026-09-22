# -*- coding: utf-8 -*-
"""gold 50 채점(준거 A) — verifier 담당. gold.json(사람 라벨) 대비 S1 후보 집합 채점.

원리는 `tmp/run_demo_diag.py::match_gold()`(최대겹침 매칭)를 확장한 것 — 그 스크립트는
집계 없이 콘솔 출력만 했고, 여기서는 clip 50개 전체를 집계·오류 5코드로 분류한다.

**sparse gold 주의**: gold.json의 `human`은 present만(absent 라벨 없음). 따라서:
  - recall  = |predicted ∩ gold| / |gold|                → 신뢰 가능
  - precision_lower = |predicted ∩ gold| / |predicted|    → **하한만**. gold에 없는
    predicted 태그가 실제로 틀렸다는 뜻이 아니다(라벨 안 된 정답일 수 있음).
CLAUDE.md 원칙 5(필요조건≠정답)와 같은 취지 — precision_lower를 "정확도"로 보고하지 않는다.

predicted 는 `outputs/episodes/<clip>.json`의 S1 후보(candidates.generate_candidates
OR 합집합, 3채널)에서 뽑는다 — Phase A/B가 원래 이 vocab(road_urban_arterial 등)으로
gold와 동일 어휘를 쓰도록 설계됐기 때문(retrieve.index_clip의 병합 이전 원본 사용).

실행: ./run.sh verification/score_gold.py --exp C001
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# GT/ego 채널로 결정론적 검출 가능한 카테고리 — FN을 ERR-FACT(S1) vs ERR-VISUAL(S2)로
# 나누는 데 쓰는 근사 규칙(v1, 미세조정 전). candidates._CAUSE_OF의 agent/road_geometry
# 키 + 회전류(arc 직접)가 GT/ego로 결정되는 축이다. 나머지(신호·도로유형·주변 정황)는
# VLM 확인이 필요한 축으로 본다.
_GT_DERIVABLE = {
    "cut_in", "cut_in_attempt", "cut_out", "lead_decel", "close_follow", "ped_crossing",
    "vru_roadside", "cyclist_pm_near", "oncoming_encroach", "agent_yields_to_ego",
    "ego_yields_to_agent", "turn_left", "turn_right", "u_turn", "lane_change_left",
    "lane_change_right", "roundabout", "merge_onramp", "creep", "decel_at_intersection",
}


def _overlap(w0, w1, ow0, ow1):
    return max(0.0, min(w1, ow1) - max(w0, ow0))


def _predicted_episodes(clip_id, exp):
    p = ROOT / "outputs" / "episodes" / exp / f"{clip_id}.json"
    if not p.exists():
        return None
    d = json.loads(p.read_text())
    if not d.get("s1_ok"):
        return []
    out = []
    for ep in d.get("s1_candidate_episodes", []):
        w0, w1 = ep["win"]
        cats = set(ep.get("candidates", {}).keys())
        out.append({"w0": w0, "w1": w1, "cats": cats})
    return out


def _match(pred_eps, w0, w1):
    """gold 창(w0,w1)에 최대겹침 predicted episode. pred_eps=None(S1 실패)/[](에피소드 0)/list."""
    if not pred_eps:
        return None
    best, bov = None, 0.0
    for pe in pred_eps:
        ov = _overlap(w0, w1, pe["w0"], pe["w1"])
        if ov > bov:
            bov, best = ov, pe
    return best


def score_clip(clip_id, gold_entry, pred_eps_raw):
    eps = list(gold_entry.get("episodes", [])) + list(gold_entry.get("manual", []))
    rows = []
    for ge in eps:
        w0, w1 = ge.get("win", [0, 0])
        human = set(ge.get("human", []))
        matched = _match(pred_eps_raw, w0, w1) if pred_eps_raw is not None else None
        predicted = matched["cats"] if matched else set()

        tp = human & predicted
        fn = human - predicted
        fp = predicted - human

        errors = []
        for cat in sorted(fn):
            if pred_eps_raw is None:
                errors.append({"cat": cat, "code": "ERR-FACT|ERR-MAP",
                                "note": "S1(candidates.generate_candidates) 자체 실패 — 3DOD/지도 원인 미분리"})
            elif matched is None:
                errors.append({"cat": cat, "code": "ERR-ANCHOR",
                                "note": "이 gold 구간과 겹치는 S0 에피소드 없음 — 전이 검출 미탐"})
            elif cat in _GT_DERIVABLE:
                errors.append({"cat": cat, "code": "ERR-FACT",
                                "note": "GT/ego 결정론적 검출 대상인데 후보 집합에 없음 — S1 검출기 임계/누락"})
            else:
                errors.append({"cat": cat, "code": "ERR-VISUAL",
                                "note": "VLM 확인 축 카테고리 — S2 미확인(n-vote 5회 전부 미투표) 추정"})

        rows.append({"win": [w0, w1], "human": sorted(human), "predicted": sorted(predicted),
                      "tp": sorted(tp), "fn": sorted(fn), "fp": sorted(fp), "errors": errors,
                      "s0_matched": matched is not None, "manual_gold": bool(ge.get("manual"))})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", required=True)
    ap.add_argument("--gold", default=str(ROOT / "gold.json"))
    args = ap.parse_args()

    gold = json.loads(Path(args.gold).read_text())
    all_rows = {}
    for clip_id, entry in gold.items():
        pred_eps = _predicted_episodes(clip_id, args.exp)
        all_rows[clip_id] = score_clip(clip_id, entry, pred_eps)

    out_path = ROOT / "experiments" / "results" / args.exp / "gold_score.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(all_rows, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")

    n_gold_eps = sum(len(v) for v in all_rows.values())
    n_human = sum(len(r["human"]) for rows in all_rows.values() for r in rows)
    n_tp = sum(len(r["tp"]) for rows in all_rows.values() for r in rows)
    n_fn = sum(len(r["fn"]) for rows in all_rows.values() for r in rows)
    n_fp = sum(len(r["fp"]) for rows in all_rows.values() for r in rows)
    n_predicted = n_tp + n_fp
    n_anchor_miss = sum(1 for rows in all_rows.values() for r in rows if not r["s0_matched"])
    n_no_s1 = sum(1 for cid in gold if _predicted_episodes(cid, args.exp) is None)

    recall = round(n_tp / n_human, 3) if n_human else None
    precision_lower = round(n_tp / n_predicted, 3) if n_predicted else None

    by_code = {}
    for rows in all_rows.values():
        for r in rows:
            for e in r["errors"]:
                by_code[e["code"]] = by_code.get(e["code"], 0) + 1

    summary = {
        "n_clips": len(gold), "n_gold_episodes": n_gold_eps, "n_human_tags": n_human,
        "recall": recall, "precision_lower_bound": precision_lower,
        "precision_caveat": "sparse gold(human=present만, absent 라벨 없음) — precision은 하한만, 정확도로 보고 금지",
        "n_tp": n_tp, "n_fn": n_fn, "n_fp": n_fp,
        "n_anchor_miss_episodes": n_anchor_miss, "n_clips_s1_failed": n_no_s1,
        "error_by_code": by_code,
        "err_attrib_note": "ERR-ATTRIB(S3 인과귀속) 채점 불가 — gold.json에 cause 필드 없음(스키마 갭)",
    }
    (ROOT / "experiments" / "results" / args.exp / "gold_score_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"저장 -> {out_path}")


if __name__ == "__main__":
    main()
