"""훅 공용 유틸. 저장소 배치가 다르면 이 파일의 경로만 고친다."""
import subprocess, sys, json
from pathlib import Path

# ── 저장소 경로 (실제 배치에 맞게 수정)
# 2026-09-08 정정: 아래 경로들이 이 저장소의 실제 배치와 달라 관련 훅 4/6이 상시
# 무력화(대상 없음→항상 통과)돼 있었음. GOLD/LINT는 실제 경로로 고쳤다.
# 2026-09-09: VERIFY_DIRS·CARDS_DIR·RESULTS_DIR 디렉토리를 실제로 만들었다
# (verification/, experiments/cards/, experiments/results/ — docs/design/AGENT_DESIGN.md
# §3 인수인계 표와 동일 경로). 의존 훅(check_import_separation·check_experiment_card)이
# 이제 대상을 실제로 검사한다 — "대상 없음" 안내 출력부는 재발 방지용으로 남겨둔다.
VOCAB_GLOB   = "common/schema/tag_vocab_v*.json"
GUIDE_GLOB   = "docs/design/pipeline_design_guide_v*.md"
LINT         = "common/schema/Vocab_lint.py"
THRESHOLDS   = "common/thresholds.py"          # 임계 상수 유일 허용 파일
PIPELINE_DIRS = ["task_episode/", "task_selection/", "common/"]   # 생산 측
VERIFY_DIRS   = ["verification/"]                                 # 검증 측 — 채점 코드(gate.py·score_gold.py 등)
GOLD_DIR      = "gold_label/"                                     # 실제 gold 라벨링 산출물 위치
GOLD_FILE     = "gold.json"                                       # 최상위 gold 정답 파일
GOLD_ALLOWED  = ["task_selection/review.py", "task_episode/gold_tool.py",
                 "task_episode/extract_s0_validation.py",
                 "task_episode/extract_vis100_gold.py",
                 "task_episode/run_pipeline.py", "verification/score_gold.py",
                 ".claude/settings.local.json"]  # 도구 권한 허용목록(curl 검증 명령 텍스트에
                 # gold_label/ 경로 문자열이 우연히 포함 — 데이터 소비 아님, 2026-09-22
                 "task_episode/run_pipeline.py", "verification/score_gold.py"]
# 위 목록: gold_label/ 이 실제 gold 정답(gold.json, sample_clips/episodes.json)과
# Stage1/Stage2 일반 산출물 저장 위치를 겸하고 있어(이 저장소 관행) 함께 걸림 — 전부
# 표본제외·산출물 read/write·채점(verification 쪽)일 뿐 gold 정답을 학습·튜닝에 쓰는
# 경로가 아님(합법 참조). score_gold.py는 검증 측이므로 여기 있어도 import 분리는 위반 아님
RESULTS_DIR   = "experiments/results/"                             # 원시 결과 + 매니페스트
CARDS_DIR     = "experiments/cards/"                                # 실험 카드(사전 등록)

def staged_files():
    out = subprocess.run(["git","diff","--cached","--name-only","--diff-filter=ACMR"],
                         capture_output=True, text=True).stdout
    return [Path(p) for p in out.split() if p]

def staged_content(path):
    r = subprocess.run(["git","show",f":{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""

def latest(glob):
    hits = sorted(Path(".").glob(glob))
    return hits[-1] if hits else None

def under(path, dirs):
    s = str(path).replace("\\","/")
    return any(s.startswith(d) for d in dirs)

def fail(msg):
    print(f"\n[pre-commit 차단] {msg}\n", file=sys.stderr); sys.exit(1)