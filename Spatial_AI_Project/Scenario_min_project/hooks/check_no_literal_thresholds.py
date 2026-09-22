#!/usr/bin/env python3
"""임계 상수는 thresholds 모듈에만 둔다. 다른 파이썬 파일에서 같은 이름에 숫자를 대입하면 차단.
   근거: LEAD_IN 4중복, OBST_CUTIN_Y 2.8 vs CUTIN_Y 3.5 값 충돌 실측."""
import re, sys
from hook_utils import *

# 관리 대상 임계 이름 — thresholds 모듈에서 자동 수집
th = Path(THRESHOLDS)
names = set()
if th.exists():
    names = set(re.findall(r"^([A-Z][A-Z0-9_]{2,})\s*[:=]", th.read_text(encoding="utf-8"), re.M))
# 알려진 이름 보강 (모듈이 아직 없을 때)
names |= {"LEAD_IN","CUTIN_Y","OBST_CUTIN_Y","REACT_FLOOR","MAX_WINDOWS","VALID_FRAC",
          "LANE_W_MIN","LANE_W_MAX","EVENT_DECEL_AX","NUM_FRAMES","CELL"}

pat = re.compile(r"^\s*(" + "|".join(map(re.escape, sorted(names))) + r")\s*=\s*[-+]?\d")
bad = []
for f in staged_files():
    if f.suffix != ".py" or str(f) == THRESHOLDS: continue
    for i, line in enumerate(staged_content(f).splitlines(), 1):
        if pat.match(line):
            bad.append(f"{f}:{i}: {line.strip()}")
if bad:
    fail("임계 상수 리터럴 대입 — thresholds 모듈로 이동하고 threshold_set_id로 관리:\n  " + "\n  ".join(bad))