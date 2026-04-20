#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPENCODE_DIR="${OPENCODE_DIR:-${SCRIPT_DIR}/opencode}"
OPENCODE_REPO="${OPENCODE_REPO:-https://github.com/anomalyco/opencode.git}"
OPENCODE_COMMIT="${OPENCODE_COMMIT:-23fb5e0516c99ac04a1aa46c193efda2e1b9bb24}"  # v1.14.18, 2026-04-19
INSTALL_DEPS="${INSTALL_DEPS:-1}"

usage() {
    cat <<'EOF'
Usage:
  ./setup_opencode.sh

Environment:
  OPENCODE_DIR     Destination directory. Default: <repo_root>/opencode
  OPENCODE_REPO    Git repository to clone. Default: anomalyco/opencode
  OPENCODE_COMMIT  Commit to checkout. Default: 23fb5e0516c99ac04a1aa46c193efda2e1b9bb24 (v1.14.18)
  INSTALL_DEPS     Set to 0 to skip `bun install`. Default: 1
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

require_command() {
    local cmd="$1"
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "[ERROR] Required command not found on PATH: $cmd" >&2
        exit 1
    fi
}

require_command git

if [[ "$INSTALL_DEPS" != "0" ]]; then
    # bun installer puts the binary at ~/.bun/bin; auto-add it so users who
    # just ran the curl installer in the same shell don't have to source rc.
    if ! command -v bun >/dev/null 2>&1 && [[ -x "$HOME/.bun/bin/bun" ]]; then
        export PATH="$HOME/.bun/bin:$PATH"
    fi
    require_command bun
fi

if [[ ! -d "$OPENCODE_DIR/.git" ]]; then
    rm -rf "$OPENCODE_DIR"
    echo "Cloning OpenCode into: $OPENCODE_DIR"
    git clone "$OPENCODE_REPO" "$OPENCODE_DIR"
else
    echo "OpenCode checkout already exists: $OPENCODE_DIR"
fi

echo "Fetching latest refs..."
git -C "$OPENCODE_DIR" fetch --tags origin

echo "Checking out commit: $OPENCODE_COMMIT"
git -C "$OPENCODE_DIR" checkout "$OPENCODE_COMMIT"

if [[ "$INSTALL_DEPS" != "0" ]]; then
    # --ignore-scripts: skip native postinstall (tree-sitter-powershell) — not needed for benchmarks.
    echo "Installing OpenCode dependencies with bun..."
    (cd "$OPENCODE_DIR" && bun install --ignore-scripts)
else
    echo "Skipping dependency install because INSTALL_DEPS=0"
fi

echo ""
echo "OpenCode is ready."
echo "Path:   $OPENCODE_DIR"
echo "Commit: $(git -C "$OPENCODE_DIR" rev-parse HEAD)"

