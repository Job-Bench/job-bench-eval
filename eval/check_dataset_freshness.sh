#!/usr/bin/env bash
# Advisory only: a failed HF check must not prevent agent execution or judging.
if [[ "${JOBBENCH_SKIP_HF_CHECK:-0}" == "1" ]]; then
    exit 0
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_PYTHON="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "$CHECK_PYTHON" ]]; then
    CHECK_PYTHON="$(command -v python3 || true)"
fi
if [[ -z "$CHECK_PYTHON" ]]; then
    echo "[WARN] Cannot check HF dataset version: Python is unavailable. Continuing evaluation." >&2
elif ! "$CHECK_PYTHON" "$REPO_ROOT/scripts/check_dataset_freshness.py" --dataset-root "${1:-}"; then
    echo "[WARN] Could not check HF dataset version. Continuing evaluation." >&2
fi
exit 0
