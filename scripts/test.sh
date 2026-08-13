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

if [[ ! -f "$BACKEND_DIR/uv.lock" ]]; then
  echo "缺少 backend/uv.lock；请先运行：cd backend && uv sync --dev" >&2
  exit 1
fi

if [[ ! -f "$FRONTEND_DIR/pnpm-lock.yaml" ]]; then
  echo "缺少 frontend/pnpm-lock.yaml；无法执行可复现的前端安装。" >&2
  exit 1
fi

if [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
  echo "缺少前端依赖；请先运行：cd frontend && pnpm install --frozen-lockfile" >&2
  exit 1
fi

# The default suite is deterministic and must never make a paid provider call.
export FAKE_GENERATOR=1
export PET_FUSION_FAKE_GENERATOR=1
export FAKE_CRITIC=1
export PET_FUSION_FAKE_CRITIC=1
export RUN_OPENAI_LIVE_TESTS=0
# Mask both supported credential aliases so a developer's live shell or dotenv
# configuration cannot leak into the deterministic suite. Tests that exercise a
# live-provider constructor pass an explicit inert SecretStr instead.
export OPENAI_API_KEY=
export PET_FUSION_OPENAI_API_KEY=
export OPENAI_BASE_URL=
export PET_FUSION_OPENAI_BASE_URL=
export CI=true

echo "检查后端格式与测试"
(
  cd "$BACKEND_DIR"
  uv run --locked ruff check app tests
  uv run --locked mypy app
  uv run --locked pytest
)

echo "检查前端类型、测试与生产构建"
(
  cd "$FRONTEND_DIR"
  pnpm typecheck
  pnpm test
  pnpm build
)
