#!/usr/bin/env python3
"""gold는 학습·프롬프트 튜닝에 쓰지 않는다 (불변식).
   검증 디렉터리 밖 코드가 gold 경로를 참조하면 차단.
   2026-09-08: GOLD_DIR="data/gold/"가 실제와 달라 상시 무력화였음 — gold.json/gold_label/
   실제 패턴으로 정정. task_selection/review.py(표본에서 gold 제외)·task_episode/gold_tool.py
   (라벨링 도구 자체)는 합법 참조라 GOLD_ALLOWED로 명시 예외."""
import sys
from hook_utils import *

PATTERNS = [GOLD_DIR.rstrip("/"), GOLD_FILE]
bad = []
for f in staged_files():
    if f.suffix not in {".py",".yaml",".yml",".json",".sh"}: continue
    if under(f, VERIFY_DIRS) or under(f, ["common/schema/", "hooks/"]): continue
    if str(f) in GOLD_ALLOWED: continue
    src = staged_content(f)
    if any(p in src for p in PATTERNS):
        bad.append(str(f))
if bad:
    fail(f"검증 측(또는 GOLD_ALLOWED) 외부에서 gold 경로 참조 — 학습·튜닝 오염 위험:\n  " + "\n  ".join(bad))