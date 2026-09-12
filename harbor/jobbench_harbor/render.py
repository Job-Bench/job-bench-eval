"""Render one raw JobBench task as a self-contained Harbor 0.22 package."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path


HARBOR_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = HARBOR_ROOT / "templates"
VERIFY_PATH = HARBOR_ROOT / "runtime" / "verify.py"
SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")

INSTRUCTION = """=== TASK FOLDER ===
/workspace/task_folder

=== INSTRUCTIONS ===
1. Read the TASK_INSTRUCTIONS.txt file in the task folder above.
2. Based on the Reference Files section in TASK_INSTRUCTIONS.txt, read the corresponding files from the same task folder using appropriate tools.
3. Complete the task as specified in TASK_INSTRUCTIONS.txt.
4. Only save final deliverables to the output directory specified below. Do not save intermediate or temporary files there.

=== OUTPUT DIRECTORY ===
/workspace/output

IMPORTANT:
- All local reference files are in /workspace/task_folder.
- Only save final deliverables to /workspace/output. Do not save intermediate or temporary files there.
- If the task asks you to modify an existing database or workbook, save the final modified copy in /workspace/output; do not leave the only final copy in the task folder.
- You may access files inside /workspace and search online for new reference files if needed. Do not access other filesystem paths.
- If you encounter ambiguous or conflicting information, analyze the conflict, explain your reasoning, and justify the approach you choose.
- If a file cannot be read directly (for example .xlsx, .docx, .db, or .pptx), use appropriate tools or code to extract and process its contents.
"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_files(source_dir: Path) -> list[dict[str, str | int]]:
    selected: list[Path] = []
    for name in ("RUBRICS.json", "task_card.md"):
        path = source_dir / name
        if path.exists():
            selected.append(path)
    task_folder = source_dir / "task_folder"
    if task_folder.is_dir():
        selected.extend(path for path in task_folder.rglob("*") if path.is_file())
    records = []
    for path in sorted(selected, key=lambda item: item.relative_to(source_dir).as_posix()):
        records.append(
            {
                "path": path.relative_to(source_dir).as_posix(),
                "sha256": _sha256(path),
                "size": path.stat().st_size,
            }
        )
    return records


def _validate_tree(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Source task folder must be a real directory: {root}")
    pending = [root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISLNK(mode):
                    raise ValueError(f"Source task folder contains a symlink: {entry.path}")
                if stat.S_ISDIR(mode):
                    pending.append(Path(entry.path))
                elif not stat.S_ISREG(mode):
                    raise ValueError(f"Source task folder contains a special file: {entry.path}")


def _copy_template(name: str, destination: Path) -> None:
    shutil.copyfile(TEMPLATES / name, destination)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _task_toml(
    *, task_id: str, split: str, occupation: str, task_number: str, repo_id: str, revision: str
) -> str:
    return f'''schema_version = "1.4"
artifacts = ["/workspace/output"]

[task]
name = {_toml_string(f"jobbench/{task_id}")}
description = {_toml_string(f"JobBench {split} task: {occupation} {task_number}")}
authors = [{{ name = "JobBench" }}]
keywords = ["jobbench", {_toml_string(split)}, {_toml_string(occupation)}]

[metadata]
id = {_toml_string(task_id)}
split = {_toml_string(split)}
occupation = {_toml_string(occupation)}
task_number = {_toml_string(task_number)}
repo_id = {_toml_string(repo_id)}
revision = {_toml_string(revision)}

[agent]
timeout_sec = 3600.0
network_mode = "public"

[verifier]
timeout_sec = 3600.0
environment_mode = "separate"
network_mode = "public"
env = {{ JUDGE_API_BASE = "${{JUDGE_API_BASE:-https://api.x.ai/v1}}", JUDGE_API_KEY = "${{JUDGE_API_KEY:-}}", JUDGE_MODEL = "${{JUDGE_MODEL:-grok-4.3}}", JUDGE_MAX_WORKERS = "${{JUDGE_MAX_WORKERS:-10}}", JUDGE_MAX_RETRIES = "${{JUDGE_MAX_RETRIES:-3}}", JUDGE_TIMEOUT_PER_RUBRIC = "${{JUDGE_TIMEOUT_PER_RUBRIC:-300}}" }}

[verifier.environment]
build_timeout_sec = 1800.0
network_mode = "public"
cpus = 2
memory_mb = 4096
storage_mb = 8192
workdir = "/tests"

[environment]
build_timeout_sec = 1800.0
network_mode = "public"
cpus = 4
memory_mb = 8192
storage_mb = 20480
workdir = "/workspace"
'''


def render_task(
    source_dir: Path,
    destination: Path,
    *,
    task_id: str,
    split: str,
    occupation: str,
    task_number: str,
    repo_id: str,
    revision: str,
    judge_path: Path,
    search_files: list[str],
) -> dict:
    """Render a raw task directory and return its source provenance."""
    source_dir = Path(source_dir)
    destination = Path(destination)
    judge_path = Path(judge_path)
    for label, value in (
        ("task_id", task_id),
        ("split", split),
        ("occupation", occupation),
        ("task_number", task_number),
    ):
        if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
            raise ValueError(f"Invalid {label}: {value!r}")
    if split not in {"main", "easy"}:
        raise ValueError(f"Invalid split: {split!r}")

    task_folder = source_dir / "task_folder"
    rubric = source_dir / "RUBRICS.json"
    task_card = source_dir / "task_card.md"
    instructions = task_folder / "TASK_INSTRUCTIONS.txt"
    _validate_tree(task_folder)
    for label, path in (
        ("task instructions", instructions),
        ("rubric", rubric),
        ("task card", task_card),
        ("judge", judge_path),
    ):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing or invalid {label}: {path}")
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise ValueError(f"Destination must be an empty directory: {destination}")
    else:
        destination.mkdir(parents=True)

    environment = destination / "environment"
    tests = destination / "tests"
    environment.mkdir()
    tests.mkdir()
    shutil.copytree(task_folder, environment / "task_folder", copy_function=shutil.copyfile)
    _copy_template("environment.Dockerfile", environment / "Dockerfile")
    _copy_template("environment-requirements.txt", environment / "requirements.txt")
    _copy_template("verifier.Dockerfile", tests / "Dockerfile")
    _copy_template("verifier-requirements.txt", tests / "requirements.txt")
    _copy_template("test.sh", tests / "test.sh")
    (tests / "test.sh").chmod(0o755)
    shutil.copyfile(judge_path, tests / "judge.py")
    shutil.copyfile(rubric, tests / "RUBRICS.json")
    shutil.copyfile(VERIFY_PATH, tests / "verify.py")
    (destination / "instruction.md").write_text(INSTRUCTION, encoding="utf-8")
    (destination / "task.toml").write_text(
        _task_toml(
            task_id=task_id,
            split=split,
            occupation=occupation,
            task_number=task_number,
            repo_id=repo_id,
            revision=revision,
        ),
        encoding="utf-8",
    )

    return {
        "id": task_id,
        "split": split,
        "occupation": occupation,
        "task_number": task_number,
        "repo_id": repo_id,
        "revision": revision,
        "source_files": _source_files(source_dir),
        "search_files": list(search_files),
    }
