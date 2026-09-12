#!/usr/bin/env python3
"""Advisory, read-only comparison of local source provenance with HF main.

This checks recorded revisions, not local file integrity. Network access runs in
a disposable process so SDK retries, DNS and rate limits cannot stall evaluation.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


NETWORK_TIMEOUT = 5
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
REPO_PATTERN = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9_.-]*/)?[A-Za-z0-9][A-Za-z0-9_.-]*")
SOURCE_PATTERN = re.compile(
    r"(?:main|easy)/[a-z0-9][a-z0-9_-]*/task[1-9][0-9]*/"
    r"(?:task_card\.md|RUBRICS\.json|(?:task_folder|files_required_to_search)/.+)"
)


def read_manifest(path):
    if not path.is_file():
        raise ValueError("missing provenance")
    with path.open("rb") as stream:
        data = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValueError("oversized provenance")
    state = json.loads(data)
    if not isinstance(state, dict):
        raise ValueError("invalid provenance")
    repo_id, revision = state.get("repo_id"), state.get("revision")
    if (not isinstance(repo_id, str) or not REPO_PATTERN.fullmatch(repo_id)
            or not isinstance(revision, str) or not SHA_PATTERN.fullmatch(revision)):
        raise ValueError("missing repository or exact revision")
    return state


def dataset_provenance(dataset_root):
    """Find the owning setup state only when it covers the selected source path."""
    root = Path(dataset_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("dataset root is not a directory")
    for owner in (root, *root.parents):
        manifest = owner / ".setup/state.json"
        if not manifest.exists() and not manifest.is_symlink():
            continue
        if manifest.is_symlink() or manifest.parent.is_symlink():
            raise ValueError("ambiguous provenance")
        state = read_manifest(manifest)
        files = state.get("files")
        if state.get("version") != 1 or not isinstance(files, dict) or not files:
            raise ValueError("invalid setup state")
        if any(not isinstance(name, str) or not SOURCE_PATTERN.fullmatch(name)
               or "\\" in name or "\0" in name
               or any(part in {"", ".", ".."} for part in name.split("/"))
               or not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum)
               for name, checksum in files.items()):
            raise ValueError("invalid managed source entry")
        relative = root.relative_to(owner).as_posix()
        if relative != "." and relative.split("/", 1)[0] not in {"main", "easy"}:
            raise ValueError("custom dataset root")
        prefix = "" if relative == "." else relative + "/"
        if any(name.startswith(prefix) for name in files):
            return state
        raise ValueError("selected path is not in setup state")
    raise ValueError("missing setup state")


def lookup_worker(repo_id):
    """Emit only a validated SHA; SDK diagnostics may contain credentials."""
    try:
        with open(os.devnull, "w") as sink:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                from huggingface_hub import HfApi

                info = HfApi().dataset_info(
                    repo_id=repo_id, revision="main", files_metadata=False,
                    timeout=3,
                )
                revision = getattr(info, "sha", None)
        if isinstance(revision, str) and SHA_PATTERN.fullmatch(revision):
            print(revision)
    except Exception:
        # The parent provides a fixed advisory without leaking SDK exceptions.
        pass
    return 0


def latest_revision(repo_id):
    environment = dict(os.environ, HF_HUB_DISABLE_TELEMETRY="1",
                       PYTHONDONTWRITEBYTECODE="1")
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--_lookup", repo_id],
            env=environment, capture_output=True, text=True, timeout=NETWORK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None, "Hugging Face lookup timed out"
    except Exception:
        return None, "Hugging Face lookup unavailable"
    revision = result.stdout.strip()
    if result.returncode == 0 and SHA_PATTERN.fullmatch(revision):
        return revision, None
    return None, "Hugging Face lookup unavailable"


def main(argv=None):
    if os.environ.get("JOBBENCH_SKIP_HF_CHECK") == "1":
        return 0
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == "--_lookup":
        return lookup_worker(argv[1])
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dataset-root", type=Path, help="Selected ordinary task root")
    source.add_argument("--manifest", type=Path, help="Selected Harbor generation manifest")
    args = parser.parse_args(argv)
    setup = "./harbor/setup.sh" if args.manifest is not None else "./setup.sh"
    try:
        state = (read_manifest(args.manifest) if args.manifest is not None
                 else dataset_provenance(args.dataset_root))
    except Exception:
        print("[dataset] WARNING: cannot verify freshness: local provenance is missing, "
              f"invalid, or does not cover this task root. Run {setup} for managed data; "
              "custom datasets need their own provenance. Continuing evaluation.", file=sys.stderr)
        return 0
    revision, error = latest_revision(state["repo_id"])
    if error:
        print(f"[dataset] WARNING: cannot verify freshness: {error}. "
              f"Retry later or run {setup} to refresh. Continuing evaluation.", file=sys.stderr)
    elif revision != state["revision"]:
        print(f"[dataset] WARNING: local source revision {state['revision']} differs from "
              f"Hugging Face main {revision} ({state['repo_id']}). "
              f"Run {setup} to refresh. Continuing evaluation.", file=sys.stderr)
    else:
        print(f"[dataset] Recorded source revision matches Hugging Face main "
              f"{revision} ({state['repo_id']}).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
