#!/usr/bin/env python3
"""실험 결과를 커밋하려면 같은 id의 카드가 먼저 있어야 하고,
   카드에는 가설·변경 축(정확히 1개)·대조 조건·사전 등록 시각이 있어야 한다.
   2026-09-08: RESULTS_DIR/CARDS_DIR가 이 저장소에 아직 실재하지 않아(플레이스홀더)
   이 훅은 항상 대상 0건으로 통과한다 — 침묵 통과 대신 그 사실을 알린다."""
import re, sys
from pathlib import Path
from hook_utils import *

if not Path(RESULTS_DIR).is_dir():
    print(f"[정보] RESULTS_DIR({RESULTS_DIR}) 미실재 — 이 훅은 experiment-runner가 실제로 "
          f"결과를 쌓기 전까지 비활성 상태.", file=sys.stderr)

ids = {str(f).split(RESULTS_DIR,1)[1].split("/")[0] for f in staged_files() if under(f, [RESULTS_DIR])}
bad = []
for eid in sorted(ids):
    card = Path(CARDS_DIR) / f"{eid}.md"
    if not card.exists():
        bad.append(f"{eid}: 카드 없음 ({card})"); continue
    txt = card.read_text(encoding="utf-8")
    need = {"가설": r"가설\s*[:：]", "대조 조건": r"대조\s*조건\s*[:：]", "사전 등록 시각": r"등록\s*시각\s*[:：]\s*\d"}
    for k, pat in need.items():
        if not re.search(pat, txt): bad.append(f"{eid}: '{k}' 누락")
    axes = re.findall(r"변경\s*축\s*[:：]\s*(.+)", txt)
    if not axes: bad.append(f"{eid}: '변경 축' 누락")
    elif any(("," in a) or ("·" in a) or ("+" in a) for a in axes):
        bad.append(f"{eid}: 변경 축이 2개 이상 — 1 브랜치 1 변경축: {axes[0].strip()}")
if bad:
    fail("실험 카드 요건 미충족:\n  " + "\n  ".join(bad))