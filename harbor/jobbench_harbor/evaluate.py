"""Run an immutable JobBench generation through the pinned Harbor CLI."""

from __future__ import annotations

import argparse
import copy
from fnmatch import fnmatchcase
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Sequence
from uuid import uuid4

import yaml

from jobbench_harbor import __version__


HARBOR_VERSION = "0.22.0"
PROVENANCE_FILENAME = "jobbench_provenance.json"
_FRESHNESS_CHECK_TIMEOUT_SECONDS = 7

_DATASET_OPTIONS = frozenset(
    {
        "-p",
        "--path",
        "--task-git-url",
        "--task-git-commit",
        "-d",
        "--dataset",
        "--registry-url",
        "--registry-path",
        "--repo",
        "-t",
        "--task",
    }
)


class EvaluationError(ValueError):
    """An invocation cannot safely represent a JobBench evaluation."""


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobbench-eval",
        description=(
            "Run manifest-selected JobBench tasks with Harbor 0.22.0. "
            "Unrecognized arguments are passed directly to `harbor run`."
        ),
        epilog=(
            "Examples: --split easy -a terminus-2 -m provider/model; "
            "--config configs/example.yaml --ak max_turns=50"
        ),
    )
    parser.add_argument(
        "--split",
        choices=("main", "easy", "all"),
        default="main",
        help="JobBench split to run (default: main)",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        help="local Harbor JobConfig YAML or JSON; tasks are supplied by this wrapper",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate selection and describe the invocation without starting Harbor",
    )
    parser.add_argument(
        "-i",
        "--include-task-name",
        "--task-name",
        action="append",
        default=[],
        help="include manifest task IDs matching this glob; repeatable",
    )
    parser.add_argument(
        "-x",
        "--exclude-task-name",
        action="append",
        default=[],
        help="exclude manifest task IDs matching this glob; repeatable",
    )
    parser.add_argument(
        "-l",
        "--n-tasks",
        type=int,
        help="maximum selected tasks after include/exclude filters",
    )
    return parser


def _load_config(path: Path | None) -> tuple[dict[str, Any], dict[str, str] | None]:
    if path is None:
        return {}, None

    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise EvaluationError(f"Harbor config does not exist: {resolved}")
    try:
        raw_config = resolved.read_bytes()
        loaded = yaml.safe_load(raw_config.decode("utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise EvaluationError(f"Could not read Harbor config {resolved}: {exc}") from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise EvaluationError("Harbor config must contain a YAML/JSON mapping")
    if loaded.get("tasks") or loaded.get("datasets") or loaded.get("source_jobs"):
        raise EvaluationError(
            "Harbor config must not define 'tasks' or 'datasets'; "
            "this wrapper owns task and dataset selection"
        )
    return dict(loaded), {
        "path": str(resolved),
        "sha256": hashlib.sha256(raw_config).hexdigest(),
    }


def _reject_dataset_options(arguments: Sequence[str]) -> None:
    for argument in arguments:
        option = argument.split("=", 1)[0]
        if option in _DATASET_OPTIONS:
            raise EvaluationError(
                f"{option} conflicts with JobBench task and dataset selection"
            )
        if not argument.startswith("--"):
            for short_option in ("-p", "-d", "-t"):
                if argument.startswith(short_option) and argument != short_option:
                    raise EvaluationError(
                        f"{short_option} conflicts with JobBench task and dataset selection"
                    )


def _last_option_value(arguments: Sequence[str], *names: str) -> str | None:
    value: str | None = None
    for index, argument in enumerate(arguments):
        if argument in names:
            if index + 1 >= len(arguments) or arguments[index + 1].startswith("-"):
                raise EvaluationError(f"{argument} requires a value")
            value = arguments[index + 1]
            continue
        for name in names:
            prefix = f"{name}="
            if argument.startswith(prefix):
                value = argument[len(prefix) :]
                if not value:
                    raise EvaluationError(f"{name} requires a value")
            elif (
                len(name) == 2
                and name.startswith("-")
                and not argument.startswith("--")
                and argument.startswith(name)
                and argument != name
            ):
                value = argument[len(name) :]
    return value


def _take_option_values(
    arguments: Sequence[str], *names: str
) -> tuple[list[str], list[str]]:
    values: list[str] = []
    remaining: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in names:
            if index + 1 >= len(arguments) or arguments[index + 1].startswith("-"):
                raise EvaluationError(f"{argument} requires a value")
            values.append(arguments[index + 1])
            index += 2
            continue
        matched = False
        for name in names:
            prefix = f"{name}="
            if argument.startswith(prefix):
                value = argument[len(prefix) :]
                if not value:
                    raise EvaluationError(f"{name} requires a value")
                values.append(value)
                matched = True
                break
            if (
                len(name) == 2
                and name.startswith("-")
                and not argument.startswith("--")
                and argument.startswith(name)
                and argument != name
            ):
                values.append(argument[len(name) :])
                matched = True
                break
        if not matched:
            remaining.append(argument)
        index += 1
    return values, remaining


def _apply_model_only_overrides(
    config: dict[str, Any], passthrough: Sequence[str]
) -> list[str]:
    explicit_agent = _last_option_value(
        passthrough, "-a", "--agent", "--agent-import-path"
    )
    if explicit_agent is not None:
        return list(passthrough)

    model_names, remaining = _take_option_values(passthrough, "-m", "--model")
    if not model_names:
        return remaining
    configured_agents = config.get("agents")
    if (
        not isinstance(configured_agents, list)
        or len(configured_agents) != 1
        or not isinstance(configured_agents[0], dict)
    ):
        raise EvaluationError(
            "--model without --agent requires exactly one agent in the Harbor config"
        )

    original_agent = configured_agents[0]
    config["agents"] = []
    for model_name in model_names:
        agent = copy.deepcopy(original_agent)
        agent["model_name"] = model_name
        config["agents"].append(agent)
    return remaining


def _validate_job_name(name: object) -> str:
    if not isinstance(name, str) or not name or name in {".", ".."}:
        raise EvaluationError("job name must be a non-empty single directory name")
    if Path(name).name != name or "/" in name or "\\" in name:
        raise EvaluationError("job name must be a single directory name")
    return name


def _resolve_generation(
    root: Path,
    split: str,
    *,
    include: Sequence[str],
    exclude: Sequence[str],
    n_tasks: int | None,
) -> tuple[Path, dict[str, Any], list[dict[str, Any]], list[Path]]:
    current = root / "current"
    try:
        generation = current.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise EvaluationError(
            f"No prepared JobBench generation at {current}; run setup first"
        ) from exc

    generated_root = (root / ".generated").resolve()
    if generation.parent != generated_root:
        raise EvaluationError(f"current does not name an immutable generation: {generation}")
    manifest_path = generation / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"Could not read generation manifest: {manifest_path}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("tasks"), list):
        raise EvaluationError("Generation manifest must contain a tasks list")
    for field in ("repo_id", "revision", "judge_sha256"):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            raise EvaluationError(f"Generation manifest is missing {field}")

    selected_records: list[dict[str, Any]] = []
    task_paths: list[Path] = []
    seen_ids: set[str] = set()
    tasks_root = (generation / "tasks").resolve()
    for record in manifest["tasks"]:
        if not isinstance(record, dict):
            raise EvaluationError("Generation manifest task records must be mappings")
        task_id = record.get("id")
        task_split = record.get("split")
        if not isinstance(task_id, str) or Path(task_id).name != task_id:
            raise EvaluationError(f"Invalid task id in generation manifest: {task_id!r}")
        if task_id in seen_ids:
            raise EvaluationError(f"Duplicate task id in generation manifest: {task_id}")
        seen_ids.add(task_id)
        if task_split not in {"main", "easy"}:
            raise EvaluationError(f"Invalid split for manifest task {task_id}: {task_split!r}")
        if split != "all" and task_split != split:
            continue
        if include and not any(fnmatchcase(task_id, pattern) for pattern in include):
            continue
        if exclude and any(fnmatchcase(task_id, pattern) for pattern in exclude):
            continue
        if n_tasks is not None and len(task_paths) >= n_tasks:
            continue
        task_path = generation / "tasks" / task_id
        try:
            immutable_path = task_path.resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as exc:
            raise EvaluationError(f"Manifest task {task_id} is unavailable") from exc
        if not immutable_path.is_dir() or immutable_path.parent != tasks_root:
            raise EvaluationError(f"Manifest task {task_id} is not an immutable task directory")
        task_paths.append(immutable_path)
        selected_records.append(
            {
                key: record[key]
                for key in ("id", "split", "occupation", "task_number")
                if key in record
            }
        )
    if not task_paths:
        raise EvaluationError(
            f"No {split} tasks match the requested selection in generation {generation.name}"
        )
    return generation, manifest, selected_records, task_paths


def _check_harbor_version() -> None:
    try:
        installed = metadata.version("harbor")
    except metadata.PackageNotFoundError as exc:
        raise EvaluationError("The isolated Harbor installation is missing") from exc
    if installed != HARBOR_VERSION:
        raise EvaluationError(
            f"Expected Harbor {HARBOR_VERSION}, found {installed}; run harbor/setup.sh"
        )


def _validate_native_config(config: dict[str, Any]) -> None:
    try:
        from harbor.models.job.config import JobConfig

        JobConfig.model_validate(config)
    except Exception as exc:
        raise EvaluationError(f"Invalid Harbor job config: {exc}") from exc


def _native_executable() -> Path:
    # Keep the venv entry-point location. Resolving the Python symlink first
    # would jump to its base interpreter and could select a global Harbor.
    executable = Path(sys.executable).with_name("harbor")
    if not executable.is_file():
        raise EvaluationError(f"Pinned Harbor executable is missing: {executable}")
    return executable


def _check_dataset_freshness(integration_root: Path, generation: Path) -> None:
    """Report HF freshness for the selected generation without gating evaluation."""
    if os.environ.get("JOBBENCH_SKIP_HF_CHECK") == "1":
        return

    checker = integration_root.parent / "scripts" / "check_dataset_freshness.py"
    try:
        if not checker.is_file():
            raise FileNotFoundError(f"checker is missing: {checker}")
        completed = subprocess.run(
            [sys.executable, str(checker), "--manifest", str(generation / "manifest.json")],
            check=False,
            capture_output=True,
            text=True,
            timeout=_FRESHNESS_CHECK_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        print(f"WARNING: JobBench dataset freshness unknown: {exc}", file=sys.stderr)
        return

    for output in (completed.stdout, completed.stderr):
        if output:
            print(output, end="", file=sys.stderr)
    if completed.returncode:
        print(
            "WARNING: JobBench dataset freshness unknown: "
            f"checker exited with status {completed.returncode}",
            file=sys.stderr,
        )


def _safe_dry_run_summary(
    *, split: str, generation: Path, task_count: int, passthrough: Sequence[str]
) -> None:
    print(
        f"JobBench dry run: {task_count} task(s), split {split}, "
        f"immutable generation {generation.name}."
    )
    agent = _last_option_value(passthrough, "-a", "--agent")
    model = _last_option_value(passthrough, "-m", "--model")
    if agent:
        print(f"Harbor {HARBOR_VERSION} agent: {agent}")
    if model:
        print(f"Harbor {HARBOR_VERSION} model: {model}")
    print(
        "Would invoke the isolated `harbor run` with a private generated JobConfig "
        "and the supplied native options. Option values are omitted to avoid printing secrets."
    )


def _write_provenance(
    job_dir: Path,
    *,
    generation: Path,
    manifest: dict[str, Any],
    split: str,
    tasks: list[dict[str, Any]],
    input_config: dict[str, str] | None,
) -> None:
    adapter_path = Path(__file__).resolve()
    provenance: dict[str, Any] = {
        "schema_version": 1,
        "source": {
            "repo_id": manifest["repo_id"],
            "revision": manifest["revision"],
        },
        "judge_sha256": manifest["judge_sha256"],
        "generation_id": generation.name,
        "split": split,
        "tasks": tasks,
        "adapter": {
            "version": __version__,
            "sha256": hashlib.sha256(adapter_path.read_bytes()).hexdigest(),
        },
        "harbor": {"version": HARBOR_VERSION, "job_config": "config.json"},
    }
    if input_config is not None:
        provenance["input_config"] = input_config

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{PROVENANCE_FILENAME}.",
        suffix=".tmp",
        dir=job_dir,
        delete=False,
    ) as temporary_file:
        json.dump(provenance, temporary_file, indent=2, sort_keys=True)
        temporary_file.write("\n")
        temporary_path = Path(temporary_file.name)
    os.replace(temporary_path, job_dir / PROVENANCE_FILENAME)


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path | None = None,
    harbor_executable: Path | None = None,
) -> int:
    parser = _argument_parser()
    try:
        options, passthrough = parser.parse_known_args(argv)
        _check_harbor_version()
        _reject_dataset_options(passthrough)
        if options.n_tasks is not None and options.n_tasks < 1:
            raise EvaluationError("--n-tasks must be at least 1")

        integration_root = (
            root.resolve() if root is not None else Path(__file__).resolve().parents[1]
        )
        generation, manifest, tasks, task_paths = _resolve_generation(
            integration_root,
            options.split,
            include=options.include_task_name,
            exclude=options.exclude_task_name,
            n_tasks=options.n_tasks,
        )
        config, input_config = _load_config(options.config)
        passthrough = _apply_model_only_overrides(config, passthrough)

        cli_jobs_dir = _last_option_value(passthrough, "-o", "--jobs-dir")
        configured_jobs_dir = config.get("jobs_dir")
        jobs_dir_value = cli_jobs_dir or configured_jobs_dir or integration_root / "jobs"
        if not isinstance(jobs_dir_value, (str, os.PathLike)):
            raise EvaluationError("jobs_dir must be a filesystem path")
        jobs_dir = Path(jobs_dir_value).expanduser().resolve()

        cli_job_name = _last_option_value(passthrough, "--job-name")
        configured_job_name = config.get("job_name")
        default_job_name = f"jobbench-{uuid4().hex}"
        job_name = _validate_job_name(cli_job_name or configured_job_name or default_job_name)
        job_dir = jobs_dir / job_name
        if job_dir.exists():
            raise EvaluationError(f"Job directory already exists: {job_dir}")

        config["jobs_dir"] = str(jobs_dir)
        config["job_name"] = job_name
        config["tasks"] = [{"path": str(task_path)} for task_path in task_paths]
        config["datasets"] = []
        _validate_native_config(config)

        if "--print-config" not in passthrough:
            _check_dataset_freshness(integration_root, generation)

        if options.dry_run:
            _safe_dry_run_summary(
                split=options.split,
                generation=generation,
                task_count=len(task_paths),
                passthrough=passthrough,
            )
            return 0

        executable = (
            harbor_executable.resolve()
            if harbor_executable is not None
            else _native_executable()
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="jobbench-harbor-config-",
            suffix=".json",
            delete=False,
        ) as temporary_file:
            json.dump(config, temporary_file)
            effective_config_path = Path(temporary_file.name)
        try:
            try:
                completed = subprocess.run(
                    [
                        str(executable),
                        "run",
                        "--config",
                        str(effective_config_path),
                        *passthrough,
                    ],
                    check=False,
                )
                return_code = completed.returncode
            except KeyboardInterrupt:
                return_code = 130
        finally:
            effective_config_path.unlink(missing_ok=True)

        if job_dir.is_dir():
            try:
                _write_provenance(
                    job_dir,
                    generation=generation,
                    manifest=manifest,
                    split=options.split,
                    tasks=tasks,
                    input_config=input_config,
                )
            except OSError as exc:
                print(f"ERROR: could not write JobBench provenance: {exc}", file=sys.stderr)
                return return_code or 2
        return return_code
    except EvaluationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
