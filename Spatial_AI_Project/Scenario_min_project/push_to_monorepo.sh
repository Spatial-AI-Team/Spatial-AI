#!/usr/bin/env bash
# 로컬(이 저장소)의 추적 파일을 Spatial-AI 모노레포 서브폴더로 동기화·push.
# 중요 변경사항이 있을 때만 실행. .venv/영상/outputs 등은 .gitignore로 자동 제외.
#
# 2026-09-21: 개인 저장소(DaejunKang/Spatial-AI) → 팀 저장소(Spatial-AI-Team/Spatial-AI)로
# 이관. 앞으로 이 team 저장소(core-dev 팀 배정)에서 작업 진행.
#
# 사용법:  ./push_to_monorepo.sh "커밋 메시지"
set -euo pipefail
cd "$(dirname "$0")"
SRC="$PWD"
MSG="${1:?사용법: ./push_to_monorepo.sh \"커밋 메시지\"}"
REPO="git@github.com:Spatial-AI-Team/Spatial-AI.git"
PREFIX="Spatial_AI_Project/Scenario_min_project"

# 1) 로컬 커밋 (변경 있을 때만)
git add -A
git commit -q -m "$MSG

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>" || echo "  (로컬 변경 없음 — 기존 HEAD로 동기화)"

# 2) 모노레포 얕은 클론 → 서브폴더를 로컬 HEAD로 미러 (추적파일만)
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
git clone --depth 1 -q "$REPO" "$TMP/mono"
SUB="$TMP/mono/$PREFIX"
mkdir -p "$SUB"
find "$SUB" -mindepth 1 -not -name "__init__.py" -delete 2>/dev/null || true   # __init__.py는 보존
git archive HEAD | tar -x -C "$SUB"

# 3) 모노레포 커밋·PR (변경 있을 때만)
# main은 branch protection(PR 필수 + 승인 1건 + require_last_push_approval)이라 직접 push
# 불가(2026-09-21 실측, GH013). 브랜치를 새로 만들어 push하고 PR을 연다 — 병합은 팀원 승인 후.
cd "$TMP/mono"
git config user.name "DaejunKang"; git config user.email "djkang@katech.re.kr"
git add "$PREFIX"
if git diff --cached --quiet; then
  echo "모노레포 변경 없음 — PR 생략."
else
  N=$(git diff --cached --name-only | wc -l)
  BR="sync/scenario-min-project-$(date +%Y%m%d-%H%M%S)"
  git checkout -q -b "$BR"
  git commit -q -m "update Scenario_min_project: $MSG

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
  git push -q origin "$BR"
  PR_TITLE="update Scenario_min_project: ${MSG%%$'\n'*}"
  PR_URL=$(gh pr create --repo "$(echo "$REPO" | sed -E 's#.*:([^.]+)\.git#\1#')" \
    --base main --head "$BR" --title "$PR_TITLE" --body "$MSG" 2>&1) \
    && echo "PR 생성 완료 ($N 파일): $PR_URL" \
    || echo "PR 생성 실패 — 브랜치는 push됨($BR). 수동으로 PR을 열어주세요: $PR_URL"
fi
