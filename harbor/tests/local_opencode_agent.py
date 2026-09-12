"""Harbor custom-agent fixture that runs an exact locally built OpenCode binary.

This is test support. It deliberately keeps Harbor 0.22's native OpenCode run,
configuration, error detection, and ATIF conversion; only npm installation is
replaced by uploading a caller-supplied binary.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path

from harbor.agents.installed.opencode import OpenCode
from harbor.environments.base import BaseEnvironment


class LocalOpenCode(OpenCode):
    """Native Harbor OpenCode adapter with a local-binary install step."""

    _REMOTE_UPLOAD = "/tmp/jobbench-opencode"
    _REMOTE_BINARY = "/usr/local/bin/opencode"

    def __init__(self, *args, local_binary: str | Path, **kwargs):
        binary = Path(local_binary).expanduser().resolve()
        if not binary.is_file():
            raise FileNotFoundError(f"Local OpenCode binary not found: {binary}")
        self._local_binary = binary
        self._local_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()
        super().__init__(*args, **kwargs)

    @staticmethod
    def name() -> str:
        return "jobbench-local-opencode-fixture"

    async def install(self, environment: BaseEnvironment) -> None:
        await environment.upload_file(self._local_binary, self._REMOTE_UPLOAD)

        remote_upload = shlex.quote(self._REMOTE_UPLOAD)
        remote_binary = shlex.quote(self._REMOTE_BINARY)
        expected_hash = shlex.quote(self._local_sha256)
        await self.exec_as_root(
            environment,
            command=(
                f"install -m 0755 {remote_upload} {remote_binary} && "
                f'test "$(sha256sum {remote_binary} | cut -d " " -f 1)" = '
                f"{expected_hash} && "
                "command -v stdbuf >/dev/null"
            ),
        )

        version_result = await self.exec_as_agent(environment, command="opencode --version")
        detected_version = self.parse_version(version_result.stdout or "")
        if not detected_version:
            raise RuntimeError("local OpenCode binary returned an empty version")
        if self._version is not None and detected_version != self._version:
            raise RuntimeError(
                f"expected OpenCode {self._version}, local binary reported {detected_version}"
            )
        self._version = detected_version

        setup_dir = self.logs_dir / "setup"
        setup_dir.mkdir(parents=True, exist_ok=True)
        (setup_dir / "local_opencode_install.json").write_text(
            json.dumps(
                {
                    "adapter": f"{self.__class__.__module__}:{self.__class__.__name__}",
                    "sha256": self._local_sha256,
                    "version": detected_version,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
