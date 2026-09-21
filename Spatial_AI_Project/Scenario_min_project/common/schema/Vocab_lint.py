#!/usr/bin/env python3
"""
어휘 정합성 검사 (vocab lint)

tag_vocab_*.json 내부의 개념 중복·용어 불일치·미참조 항목을 검출한다.
문서 수정 시 한쪽만 고치고 다른 쪽이 남는 사고를 CI에서 차단하기 위한 것.

사용:
    python3 vocab_lint.py common/schema/tag_vocab_v0.4.json
    python3 vocab_lint.py <vocab> --guide docs/design/pipeline_design_guide_v0.4.md

종료 코드: 0 통과 / 1 오류 존재
"""
import json
import re
import sys
from pathlib import Path

ERRORS: list[str] = []
WARNS: list[str] = []


def err(msg: str) -> None:
    ERRORS.append(msg)


def warn(msg: str) -> None:
    WARNS.append(msg)


def walk(node, path=""):
    """(경로, 키, 값) 전수 순회"""
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else k
            yield p, k, v
            yield from walk(v, p)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f"{path}[{i}]")


# ─────────────────────────────────────────────────────────
# 검사 1. 폐기된 표현이 남아 있는가
# 결정이 뒤집힌 항목은 문자열로 잔존하기 쉽다.
RETIRED = {
    "window_end == key_frame_t": "window_end 정의 변경(거동 완료까지) 이후 폐기된 검사",
    "= key_frame_t. 이후": "구 window_end 정의 잔존",
    "미래 정보 차단": "폐기된 원칙 — 판정에는 창 전 구간 사용",
    "gt_before → gt_after": "gt_after 미채택 이후 존재하지 않는 이동",
    "GT-after 블록": "gt_after 미채택",
    "전량 CAN": "경로 유형 라벨은 지도∪VLM 해소",
    # 실험 설계 정본 v0.1 확정(2026-09-14)으로 되돌려진 결정 2건
    "현행 제약 디코딩": "현행 방식(guided_json 1회 생성)은 CoT 미통제로 재현 불가 — arm 기준선 자격 없음, 별도 1회 측정만 (실험설계 v0.1 §A.5)",
    "기준선 arm": "현행 방식을 기준선 arm으로 두는 구성 폐기 — arm 비교 대상 아님 (실험설계 v0.1 §A.5)",
    "재프리필 채점": "재프리필은 독립 arm이 아니라 A3의 후처리 옵션 — 라벨 불변, 확신도·엔트로피만 획득 (실험설계 v0.1 §A.4)",
    "재프리필 arm": "재프리필 arm 지위 폐기 — A3 후처리 옵션으로 강등 (실험설계 v0.1 §A.4)",
}


def check_retired(raw: str) -> None:
    for phrase, why in RETIRED.items():
        if phrase in raw:
            err(f"[폐기 표현] '{phrase}' 잔존 — {why}")


# ─────────────────────────────────────────────────────────
# 검사 2. 같은 개념이 두 곳에 정의되어 있는가
DUP_KEYS = ["window_start", "window_end", "key_frame_t", "boundary_reason"]


def check_duplicate_definitions(d: dict) -> None:
    for target in DUP_KEYS:
        hits = [p for p, k, _ in walk(d) if k == target]
        # 참조(see_also·note 내 언급)는 walk 대상이 아니므로 키 정의만 잡힌다
        if len(hits) > 1:
            err(f"[중복 정의] '{target}'가 {len(hits)}곳에 정의됨: {hits}")


# ─────────────────────────────────────────────────────────
# 검사 3. enum 값 명명 일관성 (같은 개념의 접미사 혼용)
NAMING_PAIRS = [
    ("budget_truncation", "budget_truncated"),
    ("prev_ego_transition", "prev_transition"),
]


def check_naming(raw: str) -> None:
    for a, b in NAMING_PAIRS:
        if a in raw and re.search(rf'"{b}"', raw):
            err(f"[명명 혼용] '{a}'와 '{b}'가 함께 사용됨 — 하나로 통일 필요")


# ─────────────────────────────────────────────────────────
# 검사 4. auto_checks가 존재하지 않는 필드를 참조하는가
def check_auto_checks(d: dict, raw: str) -> None:
    checks = d.get("auto_checks", {}).get("schema_checks", [])
    known = {k for _, k, _ in walk(d)}
    for c in checks:
        for token in re.findall(r"[a-z_][a-z0-9_]{3,}", c):
            if token in {"true", "false", "null", "type", "note", "values"}:
                continue
            if token not in known and f'"{token}"' not in raw:
                warn(f"[미확인 참조] auto_checks의 '{token}' — 어휘에 대응 키 없음 ({c})")


# ─────────────────────────────────────────────────────────
# 검사 5. 파생 뷰가 존재하지 않는 축·값을 참조하는가
def check_derived_views(d: dict, raw: str) -> None:
    views = d.get("derived_views", {})
    for group in ("core", "refined", "ego_side"):
        for v in views.get(group, []):
            combo = v.get("combo", {})
            for field, vals in combo.items():
                base = field.split(".")[-1]
                if f'"{base}"' not in raw:
                    err(f"[뷰 참조 오류] 뷰 '{v.get('code')}'가 존재하지 않는 필드 '{field}' 참조")
                if isinstance(vals, list):
                    for val in vals:
                        if f'"{val}"' not in raw:
                            warn(f"[뷰 값 미확인] 뷰 '{v.get('code')}'의 값 '{val}' — 어휘에 없음")


# ─────────────────────────────────────────────────────────
# 검사 6. 닫힌 집합 필드의 first-token 충돌 (선두 프록시)
def check_first_token(d: dict) -> None:
    from collections import defaultdict

    closed = {}
    for p, k, v in walk(d):
        if k == "values" and isinstance(v, list) and all(isinstance(x, str) for x in v):
            closed[p] = v
    total = 0
    for field, vals in closed.items():
        grp = defaultdict(list)
        for v in vals:
            grp[v[:3]].append(v)
        col = {k: g for k, g in grp.items() if len(g) > 1}
        if col:
            total += sum(len(g) for g in col.values())
    if total:
        warn(
            f"[점수화 경로] first-token 충돌 후보 {total}개 값 — "
            "해당 필드는 시퀀스 점수화 강제 (실제 토크나이저 검증 필요)"
        )


# ─────────────────────────────────────────────────────────
# 검사 7. 지침서와의 상호 참조 (선택)
def check_guide(d: dict, guide_path: Path) -> None:
    text = guide_path.read_text(encoding="utf-8")
    for phrase, why in RETIRED.items():
        if phrase in text:
            err(f"[지침서 폐기 표현] '{phrase}' 잔존 — {why}")
    for dv in d.get("design_variables", {}):
        if dv.startswith("DV-") and dv not in text:
            warn(f"[미참조] 설계 변수 {dv}가 지침서에 등장하지 않음")


# ─────────────────────────────────────────────────────────
# 검사 8. 지침서가 참조하는 어휘 키가 실제로 존재하고 비어 있지 않은가
#
# 계보 전환(구 어휘 → 신 어휘) 과정에서 참조는 살아남고 정의만 사라지는
# 사고가 실제로 발생했다(rule_uncertainty). 문서만 읽으면 정의가 있는 줄 안다.
BACKTICK = re.compile(r"`([a-z_][a-z0-9_]{4,})`")
# 어휘 키가 아닌 것들 — 코드 심볼·파일명·일반 용어
NOT_VOCAB_KEYS = {
    "threshold_set_id", "generator_version", "model_version", "schema_version",
    "input_data_version", "budget_profile", "views_used", "prefix_hash",
    "exemplar_bucket_id", "repair_attempted", "serving_batch_config",
    "physical_gate_passed", "map_dependent", "fallback_path", "camera_visible",
    "window_truncated", "key_frame_t", "resulting_state", "transition",
    "selection_score", "selection_rank", "selection_version", "selection_reason",
    "selection_applied", "merged_from", "curvature_correction_source",
    "transition_filters_passed", "none_source", "unresolved", "candidates",
    "max_num_seqs", "prompt_logprobs", "max_tokens", "logprobs",
    "vlm_window", "detect_events",
}


def check_guide_refs(d: dict, guide_path: Path, raw: str) -> None:
    text = guide_path.read_text(encoding="utf-8")
    top_keys = set(d.keys())
    all_keys = {k for _, k, _ in walk(d)}
    # enum 값·리스트 항목도 '정의된 식별자'로 취급 (키만 보면 값이 미정의로 잡힌다)
    for _, _, v in walk(d):
        if isinstance(v, str):
            all_keys.add(v)
        elif isinstance(v, list):
            all_keys.update(x for x in v if isinstance(x, str))
    for name in sorted(set(BACKTICK.findall(text))):
        if name in NOT_VOCAB_KEYS or name in all_keys:
            # 존재하더라도 값이 비었으면 끊어진 정의로 취급
            if name in top_keys and d.get(name) in (None, {}, []):
                err(f"[빈 정의] 지침서가 참조하는 '{name}'가 어휘에서 비어 있음")
            continue
        # 어휘 어디에도 없는 백틱 식별자 — 정의 누락 후보
        if name.count("_") >= 1:
            warn(f"[정의 누락 후보] 지침서의 `{name}`가 어휘에 없음 — 정의 필요 여부 확인")


# ─────────────────────────────────────────────────────────
# 검사 9. gt_only 필드와 rule_derivable 표기의 교차 정합
def check_gt_only_consistency(d: dict) -> None:
    pol = d.get("field_execution_policy", {})
    inj = pol.get("gt_injection", {})
    gt_only = inj.get("gt_only", [])
    if gt_only and "rule_derivable" not in json.dumps(d, ensure_ascii=False):
        warn("[교차 검사] gt_only 목록이 있으나 rule_derivable 표기가 어휘에 없음")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    vocab_path = Path(sys.argv[1])
    d = json.loads(vocab_path.read_text(encoding="utf-8"))
    raw = json.dumps(d, ensure_ascii=False)

    check_retired(raw)
    check_duplicate_definitions(d)
    check_naming(raw)
    check_auto_checks(d, raw)
    check_derived_views(d, raw)
    check_first_token(d)
    check_gt_only_consistency(d)

    if "--guide" in sys.argv:
        gp = Path(sys.argv[sys.argv.index("--guide") + 1])
        check_guide(d, gp)
        check_guide_refs(d, gp, raw)

    for w in WARNS:
        print(f"WARN  {w}")
    for e in ERRORS:
        print(f"ERROR {e}")
    print(f"\n오류 {len(ERRORS)}건 / 경고 {len(WARNS)}건")
    return 1 if ERRORS else 0


if __name__ == "__main__":
    sys.exit(main())