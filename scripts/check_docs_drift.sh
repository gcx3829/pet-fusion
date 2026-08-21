#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BASE_REF="${1:-}"
HEAD_REF="${2:-HEAD}"

if [[ -z "$BASE_REF" ]]; then
  if git rev-parse --verify "${HEAD_REF}^" >/dev/null 2>&1; then
    BASE_REF="${HEAD_REF}^"
  else
    BASE_REF="$(git hash-object -t tree /dev/null)"
  fi
fi

changed_files="$(git diff --name-only "$BASE_REF" "$HEAD_REF")"
capability_changes="$(printf '%s\n' "$changed_files" | grep -E '^(backend/app/|frontend/src/|scripts/|\.env\.example$|backend/(pyproject\.toml|uv\.lock)$|frontend/(package\.json|pnpm-lock\.yaml)$)' || true)"
documentation_changes="$(printf '%s\n' "$changed_files" | grep -E '^(README(\.en)?\.md|AGENTS\.md|CODEX_TASK\.md|docs/)' || true)"
commit_message="$(git log -1 --format=%B "$HEAD_REF" 2>/dev/null || true)"

failed=0

require_file() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    echo "文档漂移：能力证据文件不存在：$path" >&2
    failed=1
  fi
}

require_text() {
  local path="$1"
  local pattern="$2"
  if ! grep -Eq "$pattern" "$path"; then
    echo "文档漂移：$path 缺少能力声明：$pattern" >&2
    failed=1
  fi
}

require_file "docs/CAPABILITIES.md"
require_file "backend/app/services/photography_metadata_service.py"
require_file "backend/app/graphs/multimodal_prompt_subgraph.py"
require_file "backend/app/graphs/critic_subgraph.py"
require_file "backend/app/graphs/feedback_planner_subgraph.py"
require_file "backend/app/graphs/local_fix_graph.py"
require_file "backend/app/services/fusion_service.py"
require_file "backend/app/services/export_service.py"
require_file "frontend/src/features/fusion/FusionEditor.tsx"

require_text "docs/CAPABILITIES.md" '白名单 EXIF'
require_text "docs/CAPABILITIES.md" 'Prompt Refiner'
require_text "docs/CAPABILITIES.md" 'Global Winner'
require_text "docs/CAPABILITIES.md" 'Fusion Mask'
require_text "docs/CAPABILITIES.md" 'Local Fix'
require_text "README.md" 'FAKE_PROMPT_REFINER=1'
require_text "README.en.md" 'FAKE_PROMPT_REFINER=1'

if [[ -n "$capability_changes" && -z "$documentation_changes" ]]; then
  if ! grep -Eqi '^Docs-Impact:[[:space:]]*none([[:space:]]|$)' <<<"$commit_message"; then
    echo "文档漂移：本次提交修改了能力相关文件，但没有修改文档。" >&2
    echo "$capability_changes" >&2
    echo "请更新 docs/CAPABILITIES.md/README/QA 文档，或在确认无文档影响后添加提交尾注：Docs-Impact: none" >&2
    failed=1
  fi
fi

if (( failed != 0 )); then
  exit 1
fi

echo "能力与文档漂移检查通过：$BASE_REF..$HEAD_REF"
