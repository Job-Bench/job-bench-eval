#!/usr/bin/env python3
"""Refresh root task sources without overwriting evaluation history.

On the first run, existing files in the four canonical source locations are
adopted as managed sources. Later runs manage only paths in the saved manifest
and incoming snapshot. Retired tasks, including their history, are archived.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import uuid


SPLITS = {"dataset": "main", "dataset_easy": "easy"}
SOURCE_FILES = {"task_card.md", "RUBRICS.json"}
SOURCE_DIRS = {"task_folder", "files_required_to_search"}


def safe_parts(value):
    if not isinstance(value, str) or "\\" in value or "\0" in value:
        raise ValueError(f"unsafe path: {value!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe path: {value!r}")
    return parts


def is_source(value, splits):
    parts = safe_parts(value)
    return (len(parts) >= 4 and parts[0] in splits
            and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", parts[1]) is not None
            and re.fullmatch(r"task[1-9][0-9]*", parts[2]) is not None
            and ((len(parts) == 4 and parts[3] in SOURCE_FILES)
                 or (len(parts) > 4 and parts[3] in SOURCE_DIRS)))


def check_path(path):
    """Reject symlinks and non-directory parents, including dangling links."""
    for item in reversed([path, *path.parents]):
        if item.is_symlink():
            raise ValueError(f"refusing symlink: {item}")
        if item.exists() and item != path and not item.is_dir():
            raise ValueError(f"path parent is not a directory: {item}")
    if path.exists() and not (path.is_file() or path.is_dir()):
        raise ValueError(f"non-regular path: {path}")


def regular_files(root):
    check_path(root)
    if not root.is_dir():
        raise ValueError(f"missing source directory: {root}")
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError(f"refusing symlink: {path}")
            if not (path.is_dir() or path.is_file()):
                raise ValueError(f"non-regular path: {path}")
        for name in files:
            path = Path(directory) / name
            result[path.relative_to(root).as_posix()] = path
    return result


def digest(path, *, git=False):
    checksum = hashlib.sha1() if git else hashlib.sha256()
    if git:
        checksum.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def validate_tasks(files):
    tasks = {"/".join(name.split("/")[:3]) for name in files}
    for split in SPLITS.values():
        if not any(name.startswith(split + "/") for name in tasks):
            raise ValueError(f"snapshot has no tasks in {split} split")
    for task in sorted(tasks):
        for name in ("task_card.md", "RUBRICS.json", "task_folder/TASK_INSTRUCTIONS.txt"):
            if f"{task}/{name}" not in files:
                raise ValueError(f"missing required source: {task}/{name}")
        for name in ("task_card.md", "task_folder/TASK_INSTRUCTIONS.txt"):
            if not files[f"{task}/{name}"].read_text(encoding="utf-8").strip():
                raise ValueError(f"empty source: {task}/{name}")
        rubric = json.loads(files[f"{task}/RUBRICS.json"].read_text(encoding="utf-8"))
        entries = rubric.get("rubrics") if isinstance(rubric, dict) else None
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"invalid RUBRICS.json: {task}: expected nonempty rubrics")
        total = 0
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"invalid rubric entry: {task}")
            text, criteria, weight = (entry.get(key) for key in ("rubric", "criterion", "weight"))
            if (not isinstance(text, str) or not text.strip()
                    or not isinstance(criteria, list) or not criteria
                    or any(not isinstance(c, str) or not c.strip() for c in criteria)
                    or isinstance(weight, bool) or not isinstance(weight, (int, float))
                    or not math.isfinite(weight) or weight < 0):
                raise ValueError(f"invalid rubric text, criteria or weight: {task}")
            total += weight
        if total <= 0:
            raise ValueError(f"rubric weights must have positive total: {task}")
    return tasks


def fetch(setup, repo_id, revision, api, snapshot_downloader):
    info = api.dataset_info(repo_id=repo_id, revision=revision, files_metadata=True)
    sha = getattr(info, "sha", None)
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Hugging Face metadata did not return an exact commit SHA")
    expected = {}
    for sibling in getattr(info, "siblings", None) or []:
        name = sibling.rfilename
        safe_parts(name)  # Check all download paths before handing them to HF.
        if is_source(name, SPLITS):
            if name in expected:
                raise ValueError(f"duplicate source path: {name}")
            expected[name] = sibling
    repo_key = hashlib.sha256(repo_id.encode()).hexdigest()[:16]
    cache = setup / "cache" / repo_key / sha
    check_path(cache)
    if cache.exists():
        regular_files(cache)  # Never let the downloader follow a local cache symlink.
    downloaded = Path(snapshot_downloader(
        repo_id=repo_id, repo_type="dataset", revision=sha,
        local_dir=str(cache), allow_patterns=["dataset/**", "dataset_easy/**"]))
    if downloaded.absolute() != cache.absolute():
        raise ValueError(f"download returned an unexpected cache path: {downloaded}")
    raw = {}
    for source in SPLITS:
        for name, path in regular_files(cache / source).items():
            relative = f"{source}/{name}"
            if is_source(relative, SPLITS):
                raw[relative] = path
    if raw.keys() != expected.keys():
        missing, extra = sorted(expected.keys() - raw.keys()), sorted(raw.keys() - expected.keys())
        raise ValueError(f"download source set mismatch; missing={missing[:3]}, extra={extra[:3]}")
    files, hashes = {}, {}
    for name, path in sorted(raw.items()):
        sibling = expected[name]
        if getattr(sibling, "size", None) != path.stat().st_size:
            raise ValueError(f"download size mismatch: {name}")
        sha256 = digest(path)
        lfs_hash = getattr(getattr(sibling, "lfs", None), "sha256", None)
        if (sha256 != lfs_hash if lfs_hash else digest(path, git=True) != getattr(sibling, "blob_id", None)):
            raise ValueError(f"download hash mismatch: {name}")
        split, relative = name.split("/", 1)
        target = f"{SPLITS[split]}/{relative}"
        files[target], hashes[target] = path, sha256
    tasks = validate_tasks(files)
    return sha, files, hashes, tasks


def previous_sources(dest, state_path):
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        files = state.get("files") if isinstance(state, dict) else None
        if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(files, dict):
            raise ValueError(f"invalid setup manifest: {state_path}")
        if any(not is_source(name, SPLITS.values())
               or not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
               for name, value in files.items()):
            raise ValueError(f"invalid managed source path or hash: {state_path}")
        return set(files)
    # Legacy installation: adopt canonical roots only. An unrelated taskN
    # directory without a canonical marker is local data, not a retired task.
    files = set()
    for split in SPLITS.values():
        root = dest / split
        check_path(root)
        for task in sorted(root.glob("*/task*")):
            if not re.fullmatch(r"task[1-9][0-9]*", task.name):
                continue
            if not any((task / name).exists() or (task / name).is_symlink()
                       for name in SOURCE_FILES | SOURCE_DIRS):
                continue
            check_path(task)
            for name in sorted(SOURCE_FILES | SOURCE_DIRS):
                path = task / name
                check_path(path)
                if not path.exists():
                    continue
                candidates = regular_files(path).values() if name in SOURCE_DIRS else [path]
                for source in candidates:
                    relative = source.relative_to(dest).as_posix()
                    if not source.is_file() or not is_source(relative, SPLITS.values()):
                        raise ValueError(f"invalid legacy source: {source}")
                    files.add(relative)
    return files


def install(dest, setup, repo_id, sha, files, hashes, tasks):
    state_path = setup / "state.json"
    check_path(state_path)
    old = previous_sources(dest, state_path)
    old_tasks = {"/".join(name.split("/")[:3]) for name in old}
    retired = sorted(old_tasks - tasks)
    removed = sorted(name for name in old - files.keys()
                     if "/".join(name.split("/")[:3]) not in retired)
    changed, existing = [], set()
    for name in sorted(files.keys() | set(removed)):
        path = dest / name
        check_path(path)
        if path.exists():
            if not path.is_file():
                raise ValueError(f"managed source is not a regular file: {path}")
            existing.add(name)
        if name in files and (name not in existing or digest(path) != hashes[name]):
            changed.append(name)
    for name in retired:
        check_path(dest / name)
        if (dest / name).exists() and not (dest / name).is_dir():
            raise ValueError(f"retired task is not a directory: {dest / name}")
    removed = [name for name in removed if name in existing]
    retired = [name for name in retired if (dest / name).exists()]
    state = {"version": 1, "repo_id": repo_id, "revision": sha, "files": hashes}
    backup = None
    if changed or removed or retired:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = setup / "backups" / f"{stamp}-{uuid.uuid4().hex[:8]}"
        check_path(backup)
        backup.mkdir(parents=True)
        (backup / "refresh.json").write_text(json.dumps({
            "repo_id": repo_id, "revision": sha, "changed": changed,
            "deleted": removed, "retired_tasks": retired}, indent=2) + "\n")
    applied, moved, created_dirs = [], [], []
    with tempfile.TemporaryDirectory(prefix="stage-", dir=setup) as temporary:
        stage = Path(temporary)
        # Complete incoming copies and outgoing backups before active mutations.
        for name in changed:
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(files[name], target)
        for name in sorted((set(changed) | set(removed)) & existing):
            target = backup / "sources" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest / name, target)
        staged_state = stage / "state.json"
        staged_state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        try:
            for name in retired:
                target = backup / "retired" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(dest / name, target)
                moved.append(name)
            for name in changed:
                target = dest / name
                missing = []
                parent = target.parent
                while not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                for directory in reversed(missing):
                    directory.mkdir()
                    created_dirs.append(directory)
                os.replace(stage / name, target)
                applied.append(name)
            for name in removed:
                (dest / name).unlink()
                applied.append(name)
            os.replace(staged_state, state_path)
        except BaseException:
            # Best-effort rollback for ordinary I/O errors and interrupts. A
            # machine/process crash can require restoring the retained backup.
            try:
                for name in reversed(applied):
                    if name in existing:
                        restore = stage / "restore"
                        shutil.copy2(backup / "sources" / name, restore)
                        os.replace(restore, dest / name)
                    else:
                        (dest / name).unlink()
                for name in reversed(moved):
                    os.replace(backup / "retired" / name, dest / name)
                for directory in reversed(created_dirs):
                    directory.rmdir()
            except OSError as rollback_error:
                raise RuntimeError(f"refresh rollback failed; restore backup at {backup}: {rollback_error}") from rollback_error
            raise
    return {"revision": sha,
            "tasks": {split: sum(task.startswith(split + "/") for task in tasks) for split in SPLITS.values()},
            "files": len(files), "added": len(set(changed) - existing),
            "updated": len(set(changed) & existing), "deleted": len(removed),
            "retired_tasks": len(retired), "backup": str(backup) if backup else None}


def refresh(dest, *, repo_id="JobBench/job-bench", revision="main", api=None, snapshot_downloader=None):
    if api is None or snapshot_downloader is None:
        from huggingface_hub import HfApi, snapshot_download
        api = api or HfApi()
        snapshot_downloader = snapshot_downloader or snapshot_download
    dest = Path(dest).absolute()
    setup = dest / ".setup"
    check_path(setup)
    setup.mkdir(parents=True, exist_ok=True)
    lock = setup / "lock"
    check_path(lock)
    with lock.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another dataset refresh is already running") from exc
        sha, files, hashes, tasks = fetch(setup, repo_id, revision, api, snapshot_downloader)
        return install(dest, setup, repo_id, sha, files, hashes, tasks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-id", default=os.environ.get("DATASET_REPO_ID", "JobBench/job-bench"))
    parser.add_argument("--revision", default="main")
    parser.add_argument("--dest", type=Path, default=Path(__file__).resolve().parents[1] / "dataset")
    args = parser.parse_args()
    try:
        result = refresh(args.dest, repo_id=args.repo_id, revision=args.revision)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"[ERROR] Dataset refresh failed: {exc}", file=sys.stderr)
        return 1
    changed = any(result[key] for key in ("added", "updated", "deleted", "retired_tasks"))
    print(f"{'Refreshed' if changed else 'Up to date'}: {args.repo_id}@{result['revision']}")
    print(f"Tasks: main={result['tasks']['main']}, easy={result['tasks']['easy']}; source files={result['files']}")
    print(f"Changes: +{result['added']} updated={result['updated']} deleted={result['deleted']} retired_tasks={result['retired_tasks']}")
    if result["backup"]:
        print(f"Backup: {result['backup']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
