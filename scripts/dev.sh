#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
FRONTEND_DIR="$REPO_ROOT/frontend"

API_HOST="${PET_FUSION_API_HOST:-127.0.0.1}"
API_PORT="${PET_FUSION_API_PORT:-8000}"
WEB_HOST="${PET_FUSION_WEB_HOST:-127.0.0.1}"
WEB_PORT="${PET_FUSION_WEB_PORT:-5173}"

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

SERVICE_PIDS=()

stop_services() {
  trap - EXIT INT TERM
  for service_pid in "${SERVICE_PIDS[@]}"; do
    kill "$service_pid" 2>/dev/null || true
  done
  for service_pid in "${SERVICE_PIDS[@]}"; do
    wait "$service_pid" 2>/dev/null || true
  done
}

trap stop_services EXIT INT TERM

echo "启动 API：http://$API_HOST:$API_PORT"
(
  cd "$BACKEND_DIR"
  if [[ -f "$REPO_ROOT/.env" ]]; then
    exec uv run --locked --env-file "$REPO_ROOT/.env" \
      uvicorn app.main:app --reload --host "$API_HOST" --port "$API_PORT"
  else
    exec uv run --locked \
      uvicorn app.main:app --reload --host "$API_HOST" --port "$API_PORT"
  fi
) &
SERVICE_PIDS+=("$!")

if [[ "${PET_FUSION_SKIP_WORKER:-0}" != "1" ]]; then
  echo "启动 mock 搜索 worker"
  (
    cd "$BACKEND_DIR"
    if [[ -f "$REPO_ROOT/.env" ]]; then
      exec uv run --locked --env-file "$REPO_ROOT/.env" python -m app.worker
    else
      exec uv run --locked python -m app.worker
    fi
  ) &
  SERVICE_PIDS+=("$!")
fi

echo "启动 Web：http://$WEB_HOST:$WEB_PORT"
(
  cd "$FRONTEND_DIR"
  export VITE_DEV_API_TARGET="${VITE_DEV_API_TARGET:-http://$API_HOST:$API_PORT}"
  exec pnpm dev --host "$WEB_HOST" --port "$WEB_PORT"
) &
SERVICE_PIDS+=("$!")

while true; do
  for service_pid in "${SERVICE_PIDS[@]}"; do
    if ! kill -0 "$service_pid" 2>/dev/null; then
      wait "$service_pid"
      exit $?
    fi
  done
  sleep 1
done
