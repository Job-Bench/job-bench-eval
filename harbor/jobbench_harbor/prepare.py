from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath

from .hf_source import fetch_snapshot

try:
    from .render import render_task, verifier_inputs
except ImportError:  # The renderer is developed independently.
    render_task = None
    verifier_inputs = None


class SourceError(ValueError):
    pass


class GenerationError(RuntimeError):
    pass


_OCCUPATION_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
_TASK_RE = re.compile(r"task([1-9][0-9]*)")
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_SPLITS = (("main", "dataset"), ("easy", "dataset_easy"))


@dataclass(frozen=True)
class _TaskSource:
    task_id: str
    split: str
    occupation: str
    task_number: int
    path: Path
    source_files: list[dict[str, str]]
    search_files: list[str]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_files(base: Path, *, label: str) -> list[Path]:
    if base.is_symlink():
        raise SourceError(f"{label} contains a symlink: {base}")
    if not base.is_dir():
        raise SourceError(f"missing directory in {label}: {base}")

    result: list[Path] = []

    def visit(directory: Path) -> None:
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            path = Path(entry.path)
            if entry.is_symlink():
                raise SourceError(f"{label} contains a symlink: {path}")
            if entry.is_dir(follow_symlinks=False):
                visit(path)
            elif entry.is_file(follow_symlinks=False):
                result.append(path)
            else:
                raise SourceError(f"{label} contains a non-regular path: {path}")

    visit(base)
    return result


def _safe_remote_files(remote_files: list[str] | None) -> list[str]:
    safe: list[str] = []
    for value in remote_files or []:
        if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
            raise SourceError(f"unsafe remote path: {value!r}")
        raw_parts = value.split("/")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in ("", ".", "..") for part in raw_parts):
            raise SourceError(f"unsafe remote path: {value!r}")
        safe.append(path.as_posix())
    return sorted(set(safe))


def _search_files_by_task(remote_files: list[str]) -> dict[tuple[str, str, str], list[str]]:
    result: dict[tuple[str, str, str], list[str]] = {}
    split_names = {source: split for split, source in _SPLITS}
    for value in remote_files:
        parts = PurePosixPath(value).parts
        if len(parts) < 5 or parts[0] not in split_names:
            continue
        if parts[3] != "files_required_to_search":
            continue
        key = (split_names[parts[0]], parts[1], parts[2])
        result.setdefault(key, []).append(PurePosixPath(*parts[3:]).as_posix())
    return {key: sorted(values) for key, values in result.items()}


def _is_raw_source_path(value: str) -> bool:
    parts = PurePosixPath(value).parts
    if len(parts) < 4 or parts[0] not in {source for _, source in _SPLITS}:
        return False
    if _OCCUPATION_RE.fullmatch(parts[1]) is None or _TASK_RE.fullmatch(parts[2]) is None:
        return False
    if len(parts) == 4:
        return parts[3] in {"RUBRICS.json", "task_card.md"}
    return parts[3] == "task_folder"


def _validate_remote_source_set(
    tasks: list[_TaskSource], remote_files: list[str]
) -> None:
    split_sources = {split: source for split, source in _SPLITS}
    local = {
        (
            PurePosixPath(
                split_sources[task.split],
                task.occupation,
                f"task{task.task_number}",
                source["path"],
            ).as_posix()
        )
        for task in tasks
        for source in task.source_files
    }
    remote = {value for value in remote_files if _is_raw_source_path(value)}
    missing = sorted(remote - local)
    extra = sorted(local - remote)
    if missing:
        preview = ", ".join(missing[:3])
        raise SourceError(f"remote snapshot has missing local raw files: {preview}")
    if extra:
        preview = ", ".join(extra[:3])
        raise SourceError(f"snapshot has extra local raw files: {preview}")


def _source_hashes(task: Path) -> list[dict[str, str]]:
    required = (task / "RUBRICS.json", task / "task_card.md")
    for path in required:
        if path.is_symlink():
            raise SourceError(f"source contains a symlink: {path}")
        if not path.is_file():
            raise SourceError(f"missing required source file: {path.name} in {task}")
    folder = task / "task_folder"
    files = _regular_files(folder, label="task_folder")
    instructions = folder / "TASK_INSTRUCTIONS.txt"
    if instructions not in files:
        raise SourceError(f"missing required source file: TASK_INSTRUCTIONS.txt in {task}")
    try:
        rubric = json.loads((task / "RUBRICS.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceError(f"invalid RUBRICS.json in {task}: {exc}") from exc
    if not isinstance(rubric, dict) or not isinstance(rubric.get("rubrics"), list):
        raise SourceError(
            f"invalid RUBRICS.json in {task}: expected a non-empty rubrics list"
        )
    total_weight = 0.0
    for index, entry in enumerate(rubric["rubrics"]):
        if not isinstance(entry, dict):
            raise SourceError(
                f"invalid RUBRICS.json in {task}: rubric {index} is not an object"
            )
        text = entry.get("rubric")
        criteria = entry.get("criterion")
        weight = entry.get("weight")
        if not isinstance(text, str) or not text.strip():
            raise SourceError(
                f"invalid RUBRICS.json in {task}: rubric {index} has no rubric text"
            )
        if (
            not isinstance(criteria, list)
            or not criteria
            or any(not isinstance(item, str) or not item.strip() for item in criteria)
        ):
            raise SourceError(
                f"invalid RUBRICS.json in {task}: rubric {index} has invalid criteria"
            )
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not math.isfinite(weight)
            or weight < 0
        ):
            raise SourceError(
                f"invalid RUBRICS.json in {task}: rubric {index} has invalid weight"
            )
        total_weight += float(weight)
    if total_weight <= 0:
        raise SourceError(
            f"invalid RUBRICS.json in {task}: total rubric weight must be positive"
        )

    source_files = [*required, *files]
    return [
        {"path": path.relative_to(task).as_posix(), "sha256": _sha256(path)}
        for path in sorted(source_files, key=lambda item: item.relative_to(task).as_posix())
    ]


def _discover_tasks(snapshot: Path, remote_files: list[str]) -> list[_TaskSource]:
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise SourceError(f"snapshot is not a regular directory: {snapshot}")
    search_files = _search_files_by_task(remote_files)
    tasks: list[_TaskSource] = []
    for split, source_name in _SPLITS:
        split_root = snapshot / source_name
        if not split_root.exists():
            continue
        if split_root.is_symlink() or not split_root.is_dir():
            raise SourceError(f"source split is not a regular directory: {split_root}")
        for occupation_entry in sorted(os.scandir(split_root), key=lambda item: item.name):
            occupation = occupation_entry.name
            occupation_path = Path(occupation_entry.path)
            if occupation_entry.is_symlink():
                raise SourceError(f"source contains a symlink: {occupation_path}")
            if not occupation_entry.is_dir(follow_symlinks=False):
                raise SourceError(f"unexpected source entry: {occupation_path}")
            if not _OCCUPATION_RE.fullmatch(occupation):
                raise SourceError(f"invalid occupation directory: {occupation!r}")
            for task_entry in sorted(os.scandir(occupation_path), key=lambda item: item.name):
                task_path = Path(task_entry.path)
                if task_entry.is_symlink():
                    raise SourceError(f"source contains a symlink: {task_path}")
                match = _TASK_RE.fullmatch(task_entry.name)
                if not task_entry.is_dir(follow_symlinks=False) or match is None:
                    raise SourceError(f"invalid task directory: {task_path}")
                number = int(match.group(1))
                task_id = f"{split}--{occupation}--task{number}"
                tasks.append(
                    _TaskSource(
                        task_id=task_id,
                        split=split,
                        occupation=occupation,
                        task_number=number,
                        path=task_path,
                        source_files=_source_hashes(task_path),
                        search_files=search_files.get(
                            (split, occupation, task_entry.name), []
                        ),
                    )
                )
    if not tasks:
        raise SourceError("snapshot contains no JobBench tasks")
    return sorted(tasks, key=lambda task: task.task_id)


def _rendering_hash(root: Path) -> str:
    package = Path(__file__).resolve().parent
    candidates = [
        package / "render.py",
        root / "runtime" / "verify.py",
        root / "templates" / "environment.Dockerfile",
        root / "templates" / "environment-requirements.txt",
        root / "templates" / "verifier.Dockerfile",
        root / "templates" / "verifier-requirements.txt",
        root / "templates" / "test.sh",
    ]
    digest = hashlib.sha256(b"jobbench-harbor-render-v1\0")
    for path in candidates:
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise SourceError(f"rendering input is not a regular file: {path}")
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            relative = path.name
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    if verifier_inputs is not None:
        try:
            shared_inputs = verifier_inputs()
        except ValueError as exc:
            raise SourceError(str(exc)) from exc
        for relative, path in shared_inputs:
            digest.update(f"eval/{relative}".encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _fingerprint(
    *,
    repo_id: str,
    revision: str,
    judge_sha256: str,
    rendering_sha256: str,
    tasks: list[_TaskSource],
) -> str:
    task_inputs = [
        {
            "id": task.task_id,
            "source_files": task.source_files,
            "search_files": task.search_files,
        }
        for task in tasks
    ]
    return _fingerprint_from_records(
        repo_id=repo_id,
        revision=revision,
        judge_sha256=judge_sha256,
        rendering_sha256=rendering_sha256,
        tasks=task_inputs,
    )


def _fingerprint_from_records(
    *,
    repo_id: str,
    revision: str,
    judge_sha256: str,
    rendering_sha256: str,
    tasks: list[dict],
) -> str:
    description = {
        "format": 1,
        "repo_id": repo_id,
        "revision": revision,
        "judge_sha256": judge_sha256,
        "rendering_sha256": rendering_sha256,
        "tasks": [
            {
                "id": task["id"],
                "source_files": task["source_files"],
                "search_files": task["search_files"],
            }
            for task in tasks
        ],
    }
    encoded = json.dumps(description, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _expected_task_records(
    tasks: list[_TaskSource], *, repo_id: str, revision: str
) -> list[dict]:
    return [
        {
            "id": task.task_id,
            "split": task.split,
            "occupation": task.occupation,
            "task_number": task.task_number,
            "repo_id": repo_id,
            "revision": revision,
            "source_files": task.source_files,
            "search_files": task.search_files,
        }
        for task in tasks
    ]


def _generated_hashes(generation: Path) -> list[dict[str, str]]:
    files = _regular_files(generation / "tasks", label="generated tasks")
    return [
        {
            "path": path.relative_to(generation).as_posix(),
            "sha256": _sha256(path),
        }
        for path in sorted(files, key=lambda item: item.relative_to(generation).as_posix())
    ]


def _load_verified_generation(
    generation: Path,
    fingerprint: str,
    *,
    repo_id: str,
    revision: str,
    judge_sha256: str,
    rendering_sha256: str,
    tasks: list[_TaskSource],
) -> dict:
    manifest_path = generation / "manifest.json"
    if generation.is_symlink() or not generation.is_dir() or manifest_path.is_symlink():
        raise GenerationError(f"existing generation failed verification: {generation}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GenerationError(f"existing generation failed verification: {exc}") from exc
    expected_top_level = {
        "schema_version",
        "fingerprint",
        "repo_id",
        "revision",
        "judge_sha256",
        "rendering_sha256",
        "task_count",
        "tasks",
        "generated_files",
    }
    if not isinstance(manifest, dict) or set(manifest) != expected_top_level:
        raise GenerationError(
            "existing generation failed verification: unexpected manifest fields"
        )
    expected_tasks = _expected_task_records(tasks, repo_id=repo_id, revision=revision)
    try:
        manifest_tasks = manifest["tasks"]
        manifest_core = {
            "schema_version": manifest["schema_version"],
            "fingerprint": manifest["fingerprint"],
            "repo_id": manifest["repo_id"],
            "revision": manifest["revision"],
            "judge_sha256": manifest["judge_sha256"],
            "rendering_sha256": manifest["rendering_sha256"],
            "task_count": manifest["task_count"],
            "tasks": manifest_tasks,
        }
    except (KeyError, TypeError) as exc:
        raise GenerationError(
            f"existing generation failed verification: malformed manifest: {exc}"
        ) from exc
    expected_core = {
        "schema_version": 1,
        "fingerprint": fingerprint,
        "repo_id": repo_id,
        "revision": revision,
        "judge_sha256": judge_sha256,
        "rendering_sha256": rendering_sha256,
        "task_count": len(expected_tasks),
        "tasks": expected_tasks,
    }
    if manifest_core != expected_core:
        raise GenerationError("existing generation failed verification: provenance mismatch")
    try:
        recomputed = _fingerprint_from_records(
            repo_id=manifest_core["repo_id"],
            revision=manifest_core["revision"],
            judge_sha256=manifest_core["judge_sha256"],
            rendering_sha256=manifest_core["rendering_sha256"],
            tasks=manifest_tasks,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GenerationError(
            f"existing generation failed verification: fingerprint inputs: {exc}"
        ) from exc
    if recomputed != fingerprint:
        raise GenerationError("existing generation failed verification: fingerprint mismatch")
    try:
        actual = _generated_hashes(generation)
    except SourceError as exc:
        raise GenerationError(f"existing generation failed verification: {exc}") from exc
    if manifest.get("generated_files") != actual:
        raise GenerationError("existing generation failed verification: generated file mismatch")
    return manifest


def _validate_managed_links(root: Path) -> None:
    if root.is_symlink():
        raise GenerationError(f"generation root cannot be a symlink: {root}")
    generated = root / ".generated"
    if generated.is_symlink() or (generated.exists() and not generated.is_dir()):
        raise GenerationError(f"unexpected .generated path: {generated}")

    expected = {
        "tasks": "current/tasks",
        "manifest.json": "current/manifest.json",
    }
    for name, target in expected.items():
        path = root / name
        if path.is_symlink():
            if os.readlink(path) != target:
                raise GenerationError(f"unexpected {name} symlink: {os.readlink(path)}")
        elif path.exists():
            raise GenerationError(f"unmanaged path blocks {name}: {path}")

    current = root / "current"
    if current.is_symlink():
        value = os.readlink(current)
        parts = PurePosixPath(value).parts
        if (
            PurePosixPath(value).is_absolute()
            or len(parts) != 2
            or parts[0] != ".generated"
            or _FINGERPRINT_RE.fullmatch(parts[1]) is None
        ):
            raise GenerationError(f"unexpected current symlink: {value}")
    elif current.exists():
        raise GenerationError(f"unmanaged path blocks current: {current}")


def _atomic_symlink(target: str, path: Path) -> None:
    temporary = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    temporary.symlink_to(target)
    try:
        os.replace(temporary, path)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def _ensure_stable_links(root: Path) -> None:
    for name, target in (
        ("tasks", "current/tasks"),
        ("manifest.json", "current/manifest.json"),
    ):
        path = root / name
        if not path.is_symlink():
            _atomic_symlink(target, path)


def generate(
    snapshot: Path,
    root: Path,
    *,
    repo_id: str,
    revision: str,
    judge_path: Path,
    remote_files: list[str] | None = None,
) -> dict:
    snapshot = Path(snapshot)
    root = Path(root)
    judge_path = Path(judge_path)
    if judge_path.is_symlink() or not judge_path.is_file():
        raise SourceError(f"judge is not a regular file: {judge_path}")

    safe_remote_files = _safe_remote_files(remote_files)
    tasks = _discover_tasks(snapshot, safe_remote_files)
    if remote_files is not None:
        _validate_remote_source_set(tasks, safe_remote_files)
    judge_sha256 = _sha256(judge_path)
    rendering_sha256 = _rendering_hash(root)
    fingerprint = _fingerprint(
        repo_id=repo_id,
        revision=revision,
        judge_sha256=judge_sha256,
        rendering_sha256=rendering_sha256,
        tasks=tasks,
    )

    _validate_managed_links(root)
    root.mkdir(parents=True, exist_ok=True)
    generated_root = root / ".generated"
    generated_root.mkdir(exist_ok=True)
    generation = generated_root / fingerprint
    if generation.exists() or generation.is_symlink():
        manifest = _load_verified_generation(
            generation,
            fingerprint,
            repo_id=repo_id,
            revision=revision,
            judge_sha256=judge_sha256,
            rendering_sha256=rendering_sha256,
            tasks=tasks,
        )
        _ensure_stable_links(root)
        expected_current = f".generated/{fingerprint}"
        if not (root / "current").is_symlink() or os.readlink(root / "current") != expected_current:
            _atomic_symlink(expected_current, root / "current")
        return manifest

    if render_task is None:
        raise GenerationError("task renderer is unavailable")

    staging = Path(
        tempfile.mkdtemp(prefix=f".staging-{fingerprint}-", dir=generated_root)
    )
    try:
        tasks_root = staging / "tasks"
        tasks_root.mkdir()
        records: list[dict] = []
        for task in tasks:
            destination = tasks_root / task.task_id
            record = render_task(
                task.path,
                destination,
                task_id=task.task_id,
                split=task.split,
                occupation=task.occupation,
                task_number=f"task{task.task_number}",
                repo_id=repo_id,
                revision=revision,
                judge_path=judge_path,
                search_files=task.search_files,
            )
            if not isinstance(record, dict):
                raise GenerationError(f"renderer returned no task record for {task.task_id}")
            record.update(
                {
                    "id": task.task_id,
                    "split": task.split,
                    "occupation": task.occupation,
                    "task_number": task.task_number,
                    "repo_id": repo_id,
                    "revision": revision,
                    "source_files": task.source_files,
                    "search_files": task.search_files,
                }
            )
            records.append(record)

        generated_files = _generated_hashes(staging)
        manifest = {
            "schema_version": 1,
            "fingerprint": fingerprint,
            "repo_id": repo_id,
            "revision": revision,
            "judge_sha256": judge_sha256,
            "rendering_sha256": rendering_sha256,
            "task_count": len(records),
            "tasks": records,
            "generated_files": generated_files,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        _load_verified_generation(
            staging,
            fingerprint,
            repo_id=repo_id,
            revision=revision,
            judge_sha256=judge_sha256,
            rendering_sha256=rendering_sha256,
            tasks=tasks,
        )
        try:
            staging.rename(generation)
        except FileExistsError:
            _load_verified_generation(
                generation,
                fingerprint,
                repo_id=repo_id,
                revision=revision,
                judge_sha256=judge_sha256,
                rendering_sha256=rendering_sha256,
                tasks=tasks,
            )
    except Exception:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging)
        raise

    _ensure_stable_links(root)
    _atomic_symlink(f".generated/{fingerprint}", root / "current")
    return manifest


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare JobBench tasks for Harbor")
    parser.add_argument("--repo-id", default="JobBench/job-bench")
    parser.add_argument("--revision", default="main")
    parser.add_argument("--json", action="store_true", help="print the complete manifest")
    args = parser.parse_args(argv)

    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    generated_root = root / ".generated"
    existing_generations = (
        {
            path.name
            for path in generated_root.iterdir()
            if path.is_dir() and not path.is_symlink()
        }
        if generated_root.is_dir() and not generated_root.is_symlink()
        else set()
    )
    source = fetch_snapshot(
        root / ".cache" / "huggingface",
        repo_id=args.repo_id,
        revision=args.revision,
    )
    manifest = generate(
        source.path,
        root,
        repo_id=args.repo_id,
        revision=source.revision,
        judge_path=root.parent / "eval" / "judge.py",
        remote_files=source.remote_files,
    )
    if args.json:
        print(json.dumps(manifest, indent=2, sort_keys=True))
    else:
        fingerprint = manifest["fingerprint"]
        split_counts = Counter(record["split"] for record in manifest["tasks"])
        counts = ", ".join(
            f"{split}={split_counts.get(split, 0)}" for split in ("easy", "main")
        )
        status = "Up to date:" if fingerprint in existing_generations else "Generated"
        print(f"{status} {manifest['task_count']} tasks ({counts})")
        print(f"Revision: {manifest['revision']}")
        print(f"Generation: {generated_root / fingerprint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
