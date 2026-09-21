#!/usr/bin/env python3
"""어휘 값을 코드에 복제하는 것을 막는다.
   schema 디렉터리 밖 .py에서 어휘 enum 문자열이 한 파일에 N개 이상 리터럴로 등장하면 복제 의심.
   허용이 필요하면 파일 상단에  # vocab-literal-ok  주석."""
import re, sys
from hook_utils import *

N = 6
vocab = latest(VOCAB_GLOB)
if not vocab: sys.exit(0)
d = json.loads(vocab.read_text(encoding="utf-8"))
vals = set()
def walk(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == "values" and isinstance(v, list): vals.update(x for x in v if isinstance(x, str))
            walk(v)
    elif isinstance(o, list):
        for x in o: walk(x)
walk(d)
vals = {v for v in vals if len(v) >= 4}
bad = []
for f in staged_files():
    if f.suffix != ".py" or under(f, ["common/schema/"]): continue
    src = staged_content(f)
    if "# vocab-literal-ok" in src: continue
    lits = set(re.findall(r"""['"]([a-z][a-z0-9_]{3,})['"]""", src))
    hit = lits & vals
    if len(hit) >= N:
        bad.append(f"{f}: 어휘 값 {len(hit)}개 리터럴 — {sorted(hit)[:6]} …")
if bad:
    fail("어휘 복제 의심 — 로더 경유로 교체하거나 '# vocab-literal-ok' 명시:\n  " + "\n  ".join(bad))