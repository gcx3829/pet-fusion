#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
FRONTEND_DIR="$REPO_ROOT/frontend"

for command_name in uv pnpm; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "缺少命令：$command_name。请先按 README 的环境要求安装。" >&2
    exit 1
  fi
done

if [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
  echo "缺少前端依赖；请先运行：cd frontend && pnpm install --frozen-lockfile" >&2
  exit 1
fi

# Coverage remains deterministic and must never inherit a developer's live
# OpenAI configuration. Keep these aliases aligned with scripts/test.sh.
export FAKE_GENERATOR=1
export PET_FUSION_FAKE_GENERATOR=1
export FAKE_CRITIC=1
export PET_FUSION_FAKE_CRITIC=1
export RUN_OPENAI_LIVE_TESTS=0
export OPENAI_API_KEY=
export PET_FUSION_OPENAI_API_KEY=
export OPENAI_BASE_URL=
export PET_FUSION_OPENAI_BASE_URL=
export CI=true

echo "统计后端覆盖率（最低 85%）"
(
  cd "$BACKEND_DIR"
  uv run --locked pytest \
    --cov=app \
    --cov-report=term-missing \
    --cov-report=json:coverage/coverage.json \
    --cov-report=html:coverage/html \
    --cov-fail-under=85
)

echo "统计前端覆盖率（语句/行 70%，分支/函数 60%）"
(
  cd "$FRONTEND_DIR"
  pnpm test:coverage
)

echo "覆盖率报告：backend/coverage/html/index.html"
echo "覆盖率报告：frontend/coverage/index.html"
