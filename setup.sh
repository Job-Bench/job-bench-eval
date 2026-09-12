#!/usr/bin/env bash

# Set up the jobbench repo: Python environment + dataset.
# Every run checks Hugging Face and safely refreshes canonical task sources.
#
# The HF dataset has two splits and lands at:
#   dataset/main/<profession>/taskN/...
#   dataset/easy/<profession>/taskN/...

set -euo pipefail

REPO_ID="${DATASET_REPO_ID:-JobBench/job-bench}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FORCE="${FORCE:-0}"

usage() {
    cat <<EOF
Usage:
  ./setup.sh [--revision HF_COMMIT_OR_REF] [--repo-id ORG/DATASET]

Runs:
  1. Install the locked root Python environment in .venv/.
  2. Build the pinned Excel calculation runtime (Docker required).
  3. Resolve the HF revision and refresh both dataset splits.

Evaluation history and local files outside canonical source locations stay in
place. Replaced/deleted sources and retired tasks are backed up in dataset/.setup/.
On first use, existing task_card.md, RUBRICS.json, task_folder/ and
files_required_to_search/ contents are adopted as managed sources.

Environment:
  DATASET_REPO_ID  HF dataset repo id. Default: ${REPO_ID}
  FORCE            Deprecated; refresh is now the default and never wipes history.
  HF_TOKEN         Only needed if the dataset repo is private.
  JOBBENCH_DATASET_ONLY  Set to 1 to refresh task data without preparing the judge runtime.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "[ERROR] Required command not found on PATH: $1" >&2
        exit 1
    fi
}

require_command uv

echo "==> Installing Python dependencies (uv sync --locked)..."
cd "$SCRIPT_DIR"
UV_PROJECT_ENVIRONMENT="${SCRIPT_DIR}/.venv" uv sync --locked --project "$SCRIPT_DIR"

if [[ "${JOBBENCH_DATASET_ONLY:-0}" != "1" ]]; then
    echo "==> Preparing the pinned Excel calculation runtime..."
    bash "${SCRIPT_DIR}/eval/setup_judge.sh"
fi

echo ""
echo "==> Checking Hugging Face and refreshing task sources..."
if [[ "$FORCE" == "1" ]]; then
    echo "FORCE=1 is deprecated: every run refreshes sources and preserves history."
fi
exec "${SCRIPT_DIR}/.venv/bin/python" "${SCRIPT_DIR}/scripts/sync_dataset.py" "$@"
