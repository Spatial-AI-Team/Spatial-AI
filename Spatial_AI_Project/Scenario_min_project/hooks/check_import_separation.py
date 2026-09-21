#!/usr/bin/env python3
"""생산 측과 검증 측의 import 분리.
   검증 코드가 파이프라인 내부를 import하면 채점기가 태거에 종속되고,
   파이프라인이 검증 코드를 import하면 게이트 통과를 위해 코드를 맞추는 경로가 생긴다.
   2026-09-08: VERIFY_DIRS가 이 저장소에 아직 실재하지 않아(플레이스홀더) 이 훅은
   구조적으로 위반을 탐지할 수 없다 — 침묵 통과 대신 그 사실을 매 실행마다 알린다."""
import re, sys
from pathlib import Path
from hook_utils import *

if not any(Path(d).is_dir() for d in VERIFY_DIRS):
    print(f"[정보] VERIFY_DIRS({VERIFY_DIRS}) 중 실재하는 디렉토리 없음 — "
          f"이 훅은 검증 측 코드가 생기기 전까지 비활성 상태.", file=sys.stderr)

def modroots(dirs): return {d.rstrip("/").split("/")[-1] for d in dirs}
P, V = modroots(PIPELINE_DIRS), modroots(VERIFY_DIRS)
imp = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w\.]*)", re.M)
bad = []
for f in staged_files():
    if f.suffix != ".py": continue
    src = staged_content(f)
    roots = {m.split(".")[0] for m in imp.findall(src)}
    if under(f, VERIFY_DIRS) and roots & (P - {"common"}):
        bad.append(f"{f}: 검증 코드가 파이프라인 모듈 import — {sorted(roots & P)}")
    if under(f, PIPELINE_DIRS) and roots & V:
        bad.append(f"{f}: 파이프라인이 검증 모듈 import — {sorted(roots & V)}")
if bad:
    fail("생산/검증 import 분리 위반 (공용 common/ 제외):\n  " + "\n  ".join(bad))