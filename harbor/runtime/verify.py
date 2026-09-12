#!/usr/bin/env python3
"""Run the byte-identical JobBench judge and publish only valid Harbor rewards."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


DEFAULT_API_BASE = "https://api.x.ai/v1"
DEFAULT_MODEL = "grok-4.3"
SUCCESSFUL_PARSE_STATUSES = {
    "direct_json",
    "markdown_fence",
    "first_last_brace",
    "regex_extract",
}


class VerificationError(RuntimeError):
    """An infrastructure or judge-protocol failure, rather than a zero score."""


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.chmod(0o644)
    os.replace(temporary, path)


def _positive_int_env(name: str, default: int) -> str:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise VerificationError(f"{name} must be an integer") from exc
    if value < 1:
        raise VerificationError(f"{name} must be at least 1")
    return str(value)


def _inspect_output_tree(output_dir: Path) -> bool:
    """Reject links and special files; return whether any regular file exists."""
    if output_dir.is_symlink():
        raise VerificationError(f"Output directory is a symlink: {output_dir}")
    if not output_dir.exists():
        return False
    root_mode = output_dir.lstat().st_mode
    if not stat.S_ISDIR(root_mode):
        raise VerificationError(f"Output path is not a directory: {output_dir}")

    has_files = False
    pending = [output_dir]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                path = Path(entry.path)
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISLNK(mode):
                    raise VerificationError(f"Output contains a symlink: {path}")
                if stat.S_ISDIR(mode):
                    pending.append(path)
                elif stat.S_ISREG(mode):
                    has_files = True
                else:
                    raise VerificationError(f"Output contains a non-regular file: {path}")
    return has_files


def _load_rubric_count(path: Path) -> int:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"Unable to read rubric file: {exc}") from exc
    if not isinstance(payload, dict):
        raise VerificationError("Rubric file must contain a JSON object")
    rubrics = payload.get("rubrics") or payload.get("evaluation_rubrics") or []
    if not isinstance(rubrics, list) or not rubrics:
        raise VerificationError("Rubric file contains no rubrics")
    return len(rubrics)


def _read_json_object(path: Path, label: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"Unable to read {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise VerificationError(f"{label} must be a JSON object")
    return payload


def _validate_judge_logs(rubric_log_dir: Path, rubric_count: int) -> None:
    logs = sorted(rubric_log_dir.glob("jobbench_rubric_*.log"))
    if len(logs) != rubric_count:
        raise VerificationError(
            f"Judge produced {len(logs)} rubric logs for {rubric_count} rubrics"
        )
    for path in logs:
        content = path.read_text(encoding="utf-8", errors="replace")
        parse_match = re.search(r"^Parse Status: (.+)$", content, re.MULTILINE)
        exit_match = re.search(r"^API Exit Code: (.+)$", content, re.MULTILINE)
        parse_status = parse_match.group(1).strip() if parse_match else "missing"
        api_exit = exit_match.group(1).strip() if exit_match else "missing"
        if api_exit != "0" or parse_status not in SUCCESSFUL_PARSE_STATUSES:
            error_match = re.search(r"^Error: (.+)$", content, re.MULTILINE)
            detail = f": {error_match.group(1).strip()}" if error_match else ""
            raise VerificationError(
                f"Judge failed for {path.name}: API exit {api_exit}, "
                f"parse status {parse_status}{detail}"
            )
        raw_match = re.search(
            r"^RAW API RESPONSE\n=+\n(.*?)\n\n=+\nFINAL RESULT$",
            content,
            re.MULTILINE | re.DOTALL,
        )
        criteria_match = re.search(r"^Criteria Count: (\d+)$", content, re.MULTILINE)
        if raw_match is None or criteria_match is None:
            raise VerificationError(f"Judge response schema metadata is missing in {path.name}")
        response = _parse_judge_response(raw_match.group(1), parse_status)
        expected_criteria = int(criteria_match.group(1))
        criteria = response.get("criteria_results")
        if (
            not isinstance(criteria, list)
            or len(criteria) != expected_criteria
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("passed"), bool)
                for item in criteria
            )
            or not isinstance(response.get("rubric_passed"), bool)
            or not isinstance(response.get("overall_reasoning"), str)
        ):
            raise VerificationError(f"Judge response schema is invalid in {path.name}")


def _parse_judge_response(raw: str, parse_status: str) -> dict:
    candidate = raw.strip()
    if parse_status == "markdown_fence":
        match = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", candidate, re.DOTALL)
        if match is None:
            raise VerificationError("Judge response schema could not be recovered")
        candidate = match.group(1).strip()
    elif parse_status == "first_last_brace":
        first = candidate.find("{")
        last = candidate.rfind("}")
        if first < 0 or last <= first:
            raise VerificationError("Judge response schema could not be recovered")
        candidate = candidate[first : last + 1]
    elif parse_status == "regex_extract":
        candidates = re.findall(
            r'\{.*?"criteria_results"\s*:\s*\[.*?\].*?\}', candidate, re.DOTALL
        )
        for extracted in reversed(candidates):
            try:
                payload = json.loads(extracted)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
        raise VerificationError("Judge response schema could not be recovered")
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise VerificationError(f"Judge response schema is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise VerificationError("Judge response schema must be an object")
    return payload


def _validated_reward(candidate: Path) -> dict[str, float]:
    payload = _read_json_object(candidate, "judge reward")
    if set(payload) != {"reward"}:
        raise VerificationError("Judge reward must contain only the reward key")
    value = payload["reward"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VerificationError("Judge reward is not numeric")
    reward = float(value)
    if not math.isfinite(reward) or not 0.0 <= reward <= 1.0:
        raise VerificationError(f"Judge reward is out of range: {reward}")
    return {"reward": reward}


def verify(
    *, output_dir: Path, rubrics_file: Path, judge_path: Path, logs_dir: Path
) -> dict[str, float]:
    logs_dir.mkdir(parents=True, exist_ok=True)
    reward_path = logs_dir / "reward.json"
    candidate_path = logs_dir / ".judge-reward.json"
    details_path = logs_dir / "judge-details.json"
    lock_path = logs_dir / ".judge-details.lock"
    rubric_log_dir = logs_dir / "rubrics"
    for stale in (reward_path, candidate_path, details_path, lock_path):
        stale.unlink(missing_ok=True)
    if rubric_log_dir.exists():
        shutil.rmtree(rubric_log_dir)
    rubric_log_dir.mkdir()

    has_output = _inspect_output_tree(output_dir)
    rubric_count = _load_rubric_count(rubrics_file)
    if not judge_path.is_file() or judge_path.is_symlink():
        raise VerificationError(f"Original judge is unavailable: {judge_path}")

    judge_env = os.environ.copy()
    judge_env.setdefault("JUDGE_MODEL", DEFAULT_MODEL)
    judge_env.setdefault("JUDGE_API_BASE", DEFAULT_API_BASE)
    command = [
        sys.executable,
        str(judge_path),
        "--output-dir",
        str(output_dir),
        "--rubrics-file",
        str(rubrics_file),
        "--result-file",
        str(candidate_path),
        "--details-file",
        str(details_path),
        "--lock-file",
        str(lock_path),
        "--detail-log-dir",
        str(rubric_log_dir),
        "--detail-log-prefix",
        "jobbench",
        "--max-workers",
        _positive_int_env("JUDGE_MAX_WORKERS", 10),
        "--max-retries",
        _positive_int_env("JUDGE_MAX_RETRIES", 3),
        "--timeout-per-rubric",
        _positive_int_env("JUDGE_TIMEOUT_PER_RUBRIC", 300),
        "--evaluated-model",
        os.environ.get("JUDGE_EVALUATED_MODEL", "agent"),
    ]
    completed = subprocess.run(command, env=judge_env, capture_output=True, text=True)
    (logs_dir / "judge.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (logs_dir / "judge.stderr.log").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise VerificationError(f"Original judge exited with status {completed.returncode}")

    details = _read_json_object(details_path, "judge details")
    if details.get("error"):
        raise VerificationError(f"Original judge reported an error: {details['error']}")
    details_rubrics = details.get("rubrics")
    if not isinstance(details_rubrics, list) or len(details_rubrics) != rubric_count:
        raise VerificationError(
            f"Judge details contain {len(details_rubrics) if isinstance(details_rubrics, list) else 0} "
            f"results for {rubric_count} rubrics"
        )
    if has_output:
        _validate_judge_logs(rubric_log_dir, rubric_count)

    reward = _validated_reward(candidate_path)
    if not has_output and reward["reward"] != 0.0:
        raise VerificationError("Empty output must receive reward 0")
    _write_json(reward_path, reward)
    candidate_path.unlink(missing_ok=True)
    return reward


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("/workspace/output"))
    parser.add_argument("--rubrics-file", type=Path, default=Path("/tests/RUBRICS.json"))
    parser.add_argument("--judge-path", type=Path, default=Path("/tests/judge.py"))
    parser.add_argument("--logs-dir", type=Path, default=Path("/logs/verifier"))
    args = parser.parse_args()
    report_path = args.logs_dir / "verification.json"
    try:
        reward = verify(
            output_dir=args.output_dir,
            rubrics_file=args.rubrics_file,
            judge_path=args.judge_path,
            logs_dir=args.logs_dir,
        )
    except Exception as exc:
        args.logs_dir.mkdir(parents=True, exist_ok=True)
        (args.logs_dir / "reward.json").unlink(missing_ok=True)
        (args.logs_dir / ".judge-reward.json").unlink(missing_ok=True)
        _write_json(report_path, {"status": "error", "error": str(exc)})
        print(f"JobBench verifier infrastructure error: {exc}", file=sys.stderr)
        return 1
    _write_json(report_path, {"status": "ok", **reward})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
