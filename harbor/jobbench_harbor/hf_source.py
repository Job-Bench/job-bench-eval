from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Callable, Protocol


ALLOW_PATTERNS = [
    "dataset/*/task*/task_folder/**",
    "dataset/*/task*/RUBRICS.json",
    "dataset/*/task*/task_card.md",
    "dataset_easy/*/task*/task_folder/**",
    "dataset_easy/*/task*/RUBRICS.json",
    "dataset_easy/*/task*/task_card.md",
]
_OCCUPATION_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
_TASK_RE = re.compile(r"task[1-9][0-9]*")


@dataclass(frozen=True)
class HFSnapshot:
    path: Path
    revision: str
    remote_files: list[str]


class DatasetApi(Protocol):
    def dataset_info(self, repo_id: str, **kwargs: object) -> object: ...


def _is_raw_source_path(value: str) -> bool:
    parts = PurePosixPath(value).parts
    if len(parts) < 4 or parts[0] not in {"dataset", "dataset_easy"}:
        return False
    if _OCCUPATION_RE.fullmatch(parts[1]) is None or _TASK_RE.fullmatch(parts[2]) is None:
        return False
    if len(parts) == 4:
        return parts[3] in {"RUBRICS.json", "task_card.md"}
    return parts[3] == "task_folder"


def _local_raw_files(snapshot: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for source_name in ("dataset", "dataset_easy"):
        source_root = snapshot / source_name
        if not source_root.exists():
            continue
        if source_root.is_symlink() or not source_root.is_dir():
            raise RuntimeError(f"downloaded source contains an invalid path: {source_root}")
        for directory, names, filenames in os.walk(source_root, followlinks=False):
            current = Path(directory)
            for name in [*names, *filenames]:
                path = current / name
                if path.is_symlink():
                    raise RuntimeError(f"downloaded source contains a symlink: {path}")
            for filename in filenames:
                path = current / filename
                relative = path.relative_to(snapshot).as_posix()
                if _is_raw_source_path(relative):
                    result[relative] = path
    return result


def _git_blob_sha1(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def _verify_download(snapshot: Path, siblings: list[object]) -> None:
    expected = {
        name: sibling
        for sibling in siblings
        if isinstance((name := getattr(sibling, "rfilename", None)), str)
        and _is_raw_source_path(name)
    }
    actual = _local_raw_files(snapshot)
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing or extra:
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing[:3])}")
        if extra:
            detail.append(f"extra {', '.join(extra[:3])}")
        raise RuntimeError(
            "downloaded raw source set does not match Hugging Face metadata: "
            + "; ".join(detail)
        )

    for name, sibling in expected.items():
        data = actual[name].read_bytes()
        expected_size = getattr(sibling, "size", None)
        if not isinstance(expected_size, int) or expected_size != len(data):
            raise RuntimeError(f"downloaded source does not match size metadata: {name}")
        lfs = getattr(sibling, "lfs", None)
        lfs_sha256 = getattr(lfs, "sha256", None) if lfs is not None else None
        if isinstance(lfs_sha256, str):
            digest = hashlib.sha256(data).hexdigest()
            if digest != lfs_sha256:
                raise RuntimeError(f"downloaded source does not match LFS metadata: {name}")
        else:
            blob_id = getattr(sibling, "blob_id", None)
            if not isinstance(blob_id, str) or _git_blob_sha1(data) != blob_id:
                raise RuntimeError(f"downloaded source does not match Git metadata: {name}")


def fetch_snapshot(
    cache_dir: Path,
    *,
    repo_id: str,
    revision: str,
    api: DatasetApi | None = None,
    snapshot_downloader: Callable[..., str] | None = None,
    retries: int = 3,
    retry_delay: float = 0.25,
) -> HFSnapshot:
    """Resolve *revision* once and materialize only raw task inputs.

    ``local_dir`` is deliberate: Hugging Face cache snapshots contain symlinks,
    while generation accepts only regular source files and directories.
    """
    if retries < 1:
        raise ValueError("retries must be at least 1")

    if api is None or snapshot_downloader is None:
        from huggingface_hub import HfApi, snapshot_download

        api = api or HfApi()
        snapshot_downloader = snapshot_downloader or snapshot_download

    cache_dir = Path(cache_dir)
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            info = api.dataset_info(
                repo_id=repo_id,
                revision=revision,
                files_metadata=True,
            )
            sha = getattr(info, "sha", None)
            if not isinstance(sha, str) or not sha:
                raise RuntimeError("Hugging Face dataset metadata did not include a commit SHA")
            siblings = getattr(info, "siblings", None) or []
            remote_files = sorted(
                name
                for sibling in siblings
                if isinstance((name := getattr(sibling, "rfilename", None)), str)
            )
            local_dir = cache_dir / sha
            downloaded = snapshot_downloader(
                repo_id=repo_id,
                repo_type="dataset",
                revision=sha,
                local_dir=str(local_dir),
                allow_patterns=list(ALLOW_PATTERNS),
            )
            path = Path(downloaded)
            if not path.is_dir():
                raise RuntimeError(f"Hugging Face download returned no directory: {path}")
            _verify_download(path, list(siblings))
            return HFSnapshot(path=path, revision=sha, remote_files=remote_files)
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries and retry_delay:
                time.sleep(retry_delay * (2**attempt))

    assert last_error is not None
    raise last_error
