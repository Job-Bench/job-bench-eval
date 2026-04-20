#!/usr/bin/env bash

# Set up the jobbench repo: Python environment + dataset.
# Safe to re-run: `uv sync` is a no-op when deps are already installed,
# and the dataset download is skipped when dataset/ is already populated.

set -euo pipefail

REPO_ID="${DATASET_REPO_ID:-JobBench/job-bench-main}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${SCRIPT_DIR}/dataset"
FORCE="${FORCE:-0}"

usage() {
    cat <<EOF
Usage:
  ./setup.sh

Runs:
  1. uv sync --locked        (install Python deps and the 'hf' CLI)
  2. hf download <dataset>   (pull dataset/ from Hugging Face)

Environment:
  DATASET_REPO_ID  HF dataset repo id. Default: ${REPO_ID}
  FORCE            Set to 1 to wipe an existing dataset/ and re-download.
  HF_TOKEN         Only needed if the dataset repo is private.
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
uv sync --locked

echo ""
echo "==> Fetching dataset from Hugging Face..."

if [[ -d "$DEST" ]] && [[ -n "$(ls -A "$DEST" 2>/dev/null)" ]]; then
    if [[ "$FORCE" != "1" ]]; then
        echo "dataset/ already populated at: $DEST"
        echo "Set FORCE=1 to wipe and re-download."
        echo ""
        echo "Setup complete."
        exit 0
    fi
    echo "FORCE=1 — wiping $DEST"
    rm -rf "$DEST"
fi

mkdir -p "$DEST"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "Fetching ${REPO_ID} into staging ${STAGE}..."
uv run hf download "$REPO_ID" \
    --repo-type=dataset \
    --include 'dataset/**' \
    --local-dir "$STAGE" > /dev/null

if [[ ! -d "$STAGE/dataset" ]]; then
    echo "[ERROR] Expected $STAGE/dataset after download; got:" >&2
    ls -la "$STAGE" >&2
    exit 1
fi

mv "$STAGE/dataset/"* "$DEST/"
echo "Dataset ready at: $DEST"
echo ""
echo "Setup complete."
