#!/usr/bin/env python3
"""어휘 또는 지침서가 변경되면 vocab_lint를 실행한다. 오류가 있으면 커밋 차단.
   어느 한쪽만 바뀌어도 실행 — 두 파일의 불일치가 곧 병합 거부 사유이기 때문."""
import subprocess, sys
from hook_utils import *

files = staged_files()
touched = [f for f in files if f.match(VOCAB_GLOB.split("/")[-1]) or f.match(GUIDE_GLOB.split("/")[-1])]
if not touched:
    sys.exit(0)
vocab, guide = latest(VOCAB_GLOB), latest(GUIDE_GLOB)
if not (vocab and guide):
    fail("어휘 또는 지침서 파일을 찾을 수 없음 — hook_utils.py 경로 확인")
r = subprocess.run([sys.executable, LINT, str(vocab), "--guide", str(guide)], capture_output=True, text=True)
print(r.stdout)
if r.returncode != 0:
    fail("vocab_lint 오류 — 어휘·지침서 정합성 위반. 위 출력 참조")