# -*- coding: utf-8 -*-
"""물리 게이트(준거 B) — verifier 담당. `outputs/episodes/<exp>/`·`outputs/labels/<exp>/`의
산출물만 읽는다(파이프라인 모듈 import 금지 — check_import_separation). 산출은 `fail`/`undetermined`
두 값뿐이다. **`pass`는 만들지 않는다** — CLAUDE.md 원칙 5 "필요조건과 정답의 구분".

5 술어(`.claude/agents/verifier.md`가 CLAUDE.md 6검사를 재설계한 것 — geometric_consistency
+ proximity_range를 reachable 하나로 통합):
  precedes · observable · reachable · reaction_delay · direction_consistent

현재 S1/S2 산출물에 아래가 없어(§ DESIGN_LOG "물리 게이트는 아직 코드가 없어 미판정 상태
자체를 산출할 수 없음"의 정확한 계승) precedes·observable·reaction_delay는 **항상
undetermined**다 — 이것이 정상 산출이다(반응지연 유형별 실측 전, camera_visible 미계산,
cause_state_change_t 미계산):
  - cause_state_change_t (원인 상태변화 시각) — rule-engineer 미구현
  - camera_visible per-object 관측 플래그 — rule-engineer 미구현
reachable·direction_consistent는 현재 필드로 **모순 탐지가 가능한 부분만** fail 판정한다.

실행: ./run.sh verification/gate.py --exp C001
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "common"))   # thresholds만 — 파이프라인 모듈 import 없음(검증 분리 유지)
import thresholds as TH

UNDETERMINED_ALWAYS = {
    "precedes": "cause_state_change_t 미계산(rule-engineer S1 미구현) — t_c 자체가 없어 key_frame_t와 비교 불가",
    "observable": "camera_visible per-object 플래그 미계산(rule-engineer S1 미구현)",
    "reaction_delay": "cause_state_change_t 미계산 + 원인 유형별 [하한,상한] 미실측(threshold_set_id 없음)",
}

_TURN_KINDS = {"turn_left", "turn_right", "u_turn", "lane_change_left", "lane_change_right"}


def _load(clip_id, exp):
    ep_path = ROOT / "outputs" / "episodes" / exp / f"{clip_id}.json"
    fin_path = ROOT / "outputs" / "labels" / exp / clip_id / "final.json"
    ep = json.loads(ep_path.read_text()) if ep_path.exists() else None
    fin = json.loads(fin_path.read_text()) if fin_path.exists() else None
    return ep, fin


def _signal_evidence_present(track2_idx, key_frame_t):
    """cause=signal의 direction_consistent 대용: Track2 tags에 신호 관련 카테고리가
    같은 clip 어딘가에 있는지(창 중첩 엄격 매칭은 S1 계측 부재로 생략, 전 clip 범위로 완화)."""
    if not track2_idx:
        return None  # undetermined 사유용
    sig_cats = {"red_light_stop", "signal_go", "intersection_signalized"}
    for ep in track2_idx.get("episodes", []):
        cats = {t["cat"] for t in ep.get("tags", [])}
        if cats & sig_cats:
            return True
    return False


def gate_clip(clip_id, exp):
    ep, fin = _load(clip_id, exp)
    findings = []
    if fin is None or not fin.get("track1"):
        return {"clip_id": clip_id, "checks": [], "note": "final.json/track1 없음 — 채점 대상 아님"}

    track2_idx = fin.get("track2")
    for r in fin["track1"]:
        seg = r.get("segment_id")
        cause = r.get("cause")
        arc = (r.get("ego_context") or {}).get("arc", [])
        comps = r.get("critical_components") or []
        checks = {}

        for name, reason in UNDETERMINED_ALWAYS.items():
            checks[name] = {"verdict": "undetermined", "reason": reason}

        # --- reachable (geometric_consistency + proximity_range 통합) ---
        if cause == "agent":
            refs = [c["ref"] for c in comps if c.get("ref") and c["ref"].get("distance_m") is not None]
            if not refs:
                checks["reachable"] = {"verdict": "fail",
                                        "reason": "cause=agent 확정인데 거리 계측(ref.distance_m)이 있는 "
                                                   "critical_component가 없음 — 원인 후보가 도달 불가능 영역"}
            else:
                dists = sorted(d["distance_m"] for d in refs)
                near = [d for d in dists if d <= TH.GATE_PROXIMITY_MAX_M]
                if not near:
                    checks["reachable"] = {"verdict": "fail",
                                            "reason": f"모든 ref 거리 > {TH.GATE_PROXIMITY_MAX_M}m — "
                                                       f"근접성(proximity_range) 위반: {dists}"}
                else:
                    checks["reachable"] = {"verdict": "undetermined",
                                            "reason": "거리 임계는 통과 — 경로 폴리곤 대비 기하 정합(geometric_consistency)은 "
                                                       "S1 lane_relative 계측 미구현이라 미판정"}
        else:
            checks["reachable"] = {"verdict": "undetermined",
                                    "reason": f"cause={cause}는 critical_components.ref 기반 거리 계측 대상 아님 "
                                               "(현재 cause 4값 체계 — 목표 6값 static_object/road_condition 등 미분화)"}

        # --- direction_consistent ---
        if cause == "road_geometry":
            if _TURN_KINDS & set(arc):
                checks["direction_consistent"] = {"verdict": "undetermined",
                                                    "reason": "arc가 회전/차선변경류 포함 — 모순 없음(정합 자체는 별도 판정 불가)"}
            else:
                checks["direction_consistent"] = {"verdict": "fail",
                                                    "reason": f"cause=road_geometry인데 arc={arc}에 회전/차선변경류 없음"}
        elif cause == "signal":
            ev = _signal_evidence_present(track2_idx, r.get("key_frame_t"))
            if ev is None:
                checks["direction_consistent"] = {"verdict": "undetermined", "reason": "Track2 index 없음 — 신호 증거 대조 불가"}
            elif ev is False:
                checks["direction_consistent"] = {"verdict": "fail",
                                                    "reason": "cause=signal인데 Track2 인덱스에 신호 관련 카테고리 없음(클립 전체 범위 대조)"}
            else:
                checks["direction_consistent"] = {"verdict": "undetermined", "reason": "신호 증거 존재 — 세부 방향 정합은 미판정"}
        else:
            checks["direction_consistent"] = {"verdict": "undetermined",
                                                "reason": f"cause={cause}는 방향 정합 판정 규칙 미정의"}

        n_fail = sum(1 for c in checks.values() if c["verdict"] == "fail")
        findings.append({"segment_id": seg, "cause": cause, "arc": arc,
                          "checks": checks, "any_fail": n_fail > 0, "n_fail": n_fail})
    return {"clip_id": clip_id, "checks": findings}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", required=True)
    args = ap.parse_args()

    report_path = ROOT / "experiments" / "results" / args.exp / "report.json"
    results = json.loads(report_path.read_text())
    clip_ids = [r["clip_id"] for r in results]

    out = [gate_clip(cid, args.exp) for cid in clip_ids]
    out_path = ROOT / "experiments" / "results" / args.exp / "gate.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")

    n_segs = sum(len(c["checks"]) for c in out)
    n_fail_segs = sum(1 for c in out for f in c["checks"] if f["any_fail"])
    by_check = {}
    for c in out:
        for f in c["checks"]:
            for name, v in f["checks"].items():
                by_check.setdefault(name, {"fail": 0, "undetermined": 0})[v["verdict"]] += 1
    print(f"clip {len(out)}개 · segment {n_segs}개 · any_fail segment {n_fail_segs}개")
    for name, cnt in by_check.items():
        print(f"  {name:24s} fail={cnt['fail']:3d}  undetermined={cnt['undetermined']:3d}")
    print("주의: pass는 산출하지 않는다. 통과율(=undetermined 비율)을 정확도로 읽지 말 것(원칙 5).")
    print(f"저장 -> {out_path}")


if __name__ == "__main__":
    main()
