"""Launch the same bounded spreadsheet calculator locally and in Harbor."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import uuid

from .calc_worker import ENGINE

RUNTIME = Path(__file__).resolve().parents[1] / "calc-runtime" / "Dockerfile"
WORKER = Path(__file__).with_name("calc_worker.py")
TIMEOUT = 90


class ExtractionError(RuntimeError):
    """Evidence preparation failed; this is not a score of zero."""


def image_name() -> str:
    return "jobbench-calc:" + hashlib.sha256(RUNTIME.read_bytes()).hexdigest()[:20]


def setup_runtime() -> None:
    command = ["docker", "build", "-t", image_name(), str(RUNTIME.parent)]
    subprocess.run(command, check=True)


def recalculate(path: Path, cells: dict[str, list[str]], *, timeout: float = TIMEOUT) -> dict:
    """Never mount the submission directory or pass the parent environment to Calc."""
    env = {key: os.environ[key] for key in ("PATH", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")
           if key in os.environ}
    # Docker's CLI may need the user's config, but credentials/API settings are
    # never forwarded to the worker container. Native workers get only PATH/LANG.
    env["HOME"] = os.path.expanduser("~")
    native = os.environ.get("JOBBENCH_CALC_NATIVE") == "1"
    name = "jobbench-calc-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="jobbench-calc-input-") as temporary:
        source = Path(temporary) / "input.xlsx"
        source.write_bytes(path.read_bytes())
        source.chmod(0o644)
        if native:
            command = ["/usr/bin/python3", str(WORKER), str(source)]
            env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        else:
            command = [
                "docker", "run", "--rm", "-i", "--name", name, "--pull=never",
                "--network=none", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--pids-limit=128",
                "--memory=1g", "--cpus=1", "--user=65534:65534",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=1777",
                "--mount", f"type=bind,source={source},target=/input.xlsx,readonly",
                "--mount", f"type=bind,source={WORKER},target=/calc_worker.py,readonly",
                "--entrypoint=/usr/bin/python3", image_name(), "/calc_worker.py", "/input.xlsx",
            ]
        try:
            process = subprocess.Popen(command, env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, start_new_session=True)
            try:
                stdout, stderr = process.communicate(json.dumps({"cells": cells}), timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise ExtractionError(f"Excel recalculation exceeded {timeout:g} seconds")
            if process.returncode:
                if native:
                    message = stdout.strip()[:1000] or "calculation worker failed"
                else:
                    message = (stdout.strip() or stderr.strip())[:1000]
                raise ExtractionError(
                    f"Excel recalculation failed: {message}. Prepare the runtime with ./eval/setup_judge.sh."
                )
            try:
                result = json.loads(stdout)
                if result["engine"] != ENGINE or result["status"] != "recalculated":
                    raise ValueError("wrong engine or status")
                for sheet, addresses in cells.items():
                    for address in addresses:
                        value = result["cells"][sheet][address]
                        if not isinstance(value, dict) or not {"value", "display", "error_code"} <= value.keys():
                            raise ValueError("missing cell evidence")
            except (ValueError, KeyError, TypeError) as exc:
                raise ExtractionError("Excel calculation worker returned invalid evidence") from exc
            return result
        except OSError as exc:
            raise ExtractionError(
                "Excel calculation runtime unavailable; run ./eval/setup_judge.sh (Docker required)."
            ) from exc
        finally:
            if not native:
                # A killed Docker client can leave its container alive. A bounded
                # explicit cleanup removes only this invocation's unique container.
                try:
                    subprocess.run(["docker", "rm", "-f", name], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    pass
