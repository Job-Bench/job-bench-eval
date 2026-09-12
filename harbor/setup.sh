#!/usr/bin/env bash
# Provision the independent Harbor environment and refresh raw HF tasks.
set -euo pipefail

HARBOR_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    cat <<'EOF'
Usage: ./harbor/setup.sh [--revision HF_COMMIT_OR_REF] [--repo-id ORG/DATASET] [--json]

Install the locked Harbor environment under harbor/.venv, resolve the selected
Hugging Face revision (default: latest main), and generate both JobBench splits.
Every run checks the selected HF ref; unchanged files reuse the download cache.
Changed/added/deleted tasks appear together after a complete generation succeeds.
Old generations and evaluation jobs remain.
Use --json to print the full manifest instead of the concise generation summary.

Requires uv and network access. Public JobBench data needs no HF token.
EOF
    exit 0
fi
if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: uv is required. Install uv, then rerun ./harbor/setup.sh." >&2
    exit 1
fi

# Keep optional custom-agent packages installed by the evaluator. All declared
# integration dependencies are still reconciled against this directory's lock.
UV_PROJECT_ENVIRONMENT="$HARBOR_ROOT/.venv" uv sync --locked --inexact --project "$HARBOR_ROOT"
cd "$HARBOR_ROOT"
exec "$HARBOR_ROOT/.venv/bin/python" -m jobbench_harbor.prepare "$@"
