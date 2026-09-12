#!/usr/bin/env bash
# Run the unified interface using the isolated, pinned Harbor installation.
set -euo pipefail

HARBOR_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -x "$HARBOR_ROOT/.venv/bin/python" ]]; then
    echo "ERROR: Run ./harbor/setup.sh first to prepare the Harbor environment and tasks." >&2
    exit 1
fi

# Preserve the caller's working directory so native agent config/import paths
# retain their usual meaning. Put this integration on Python's module path.
export PYTHONPATH="$HARBOR_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "$HARBOR_ROOT/.venv/bin/python" -m jobbench_harbor.evaluate "$@"
