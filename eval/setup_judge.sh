#!/usr/bin/env bash
# Build the same pinned, offline spreadsheet calculator used by Harbor.
set -euo pipefail
EVAL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    echo 'Usage: ./eval/setup_judge.sh'
    echo 'Requires Docker. Builds the pinned LibreOffice runtime; no model keys or task downloads.'
    exit 0
fi
if ! command -v docker >/dev/null 2>&1; then
    echo 'ERROR: Docker is required for Excel formula recalculation. Install/start Docker, then rerun ./eval/setup_judge.sh.' >&2
    exit 1
fi
PYTHON="${EVAL_ROOT}/../.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    PYTHON="$(command -v python3)"
fi
PYTHONPATH="${EVAL_ROOT}${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" -c 'from jobbench_eval.calc import setup_runtime; setup_runtime()'
