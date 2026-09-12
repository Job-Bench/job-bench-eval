import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from harbor.environments.base import ExecResult

from local_opencode_agent import LocalOpenCode
from local_opencode_server import build_inspection_command, create_server


class RecordingEnvironment:
    default_user = "root"

    def __init__(self, version: str):
        self.version = version
        self.uploads = []
        self.commands = []

    async def upload_file(self, source_path, target_path):
        self.uploads.append((Path(source_path), target_path))

    async def exec(self, command, *, user=None, **kwargs):
        self.commands.append((command, user))
        if command.endswith("opencode --version"):
            return ExecResult(stdout=f"{self.version}\n", stderr="", return_code=0)
        return ExecResult(stdout="", stderr="", return_code=0)


class LocalOpenCodeAgentTests(unittest.TestCase):
    def test_install_uploads_and_verifies_exact_local_binary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            binary = root / "opencode"
            binary.write_bytes(b"local-opencode-binary")
            expected_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
            logs = root / "logs"
            agent = LocalOpenCode(
                logs_dir=logs,
                model_name="fixture/fixture-model",
                local_binary=binary,
                version="1.14.18",
            )
            environment = RecordingEnvironment("1.14.18")

            asyncio.run(agent.install(environment))

            self.assertEqual(environment.uploads, [(binary.resolve(), "/tmp/jobbench-opencode")])
            install_command, user = environment.commands[0]
            self.assertEqual(user, "root")
            self.assertIn("install -m 0755", install_command)
            self.assertIn(expected_hash, install_command)
            self.assertTrue(environment.commands[-1][0].endswith("opencode --version"))
            record = json.loads((logs / "setup" / "local_opencode_install.json").read_text())
            self.assertEqual(record["sha256"], expected_hash)
            self.assertEqual(record["version"], "1.14.18")

    def test_install_rejects_unexpected_binary_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            binary = root / "opencode"
            binary.write_bytes(b"local-opencode-binary")
            agent = LocalOpenCode(
                logs_dir=root / "logs",
                model_name="fixture/fixture-model",
                local_binary=binary,
                version="1.14.18",
            )

            with self.assertRaisesRegex(RuntimeError, "expected OpenCode 1.14.18"):
                asyncio.run(agent.install(RecordingEnvironment("1.14.19")))


class LocalOpenCodeServerTests(unittest.TestCase):
    def setUp(self):
        self.server = create_server("127.0.0.1", 0)
        self.thread = __import__("threading").Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def post(self, payload):
        request = urllib.request.Request(
            self.url + "/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer fixture-only"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.read().decode()

    def test_stream_requires_wire_alias_and_drives_bash_then_final(self):
        first = self.post(
            {
                "model": "fixture-wire-model",
                "stream": True,
                "messages": [{"role": "user", "content": "Complete the task"}],
            }
        )
        self.assertIn('"name":"bash"', first)
        self.assertIn("TASK_INSTRUCTIONS.txt", first)
        self.assertIn("smoke.txt", first)
        self.assertIn('"finish_reason":"tool_calls"', first)
        first_event = json.loads(first.splitlines()[0].removeprefix("data: "))
        self.assertEqual(first_event["created"], 0)

        second = self.post(
            {
                "model": "fixture-wire-model",
                "stream": True,
                "messages": [
                    {"role": "user", "content": "Complete the task"},
                    {"role": "tool", "content": "fixture tool output"},
                ],
            }
        )
        self.assertIn("Deterministic local fixture complete", second)
        self.assertIn('"finish_reason":"stop"', second)

    def test_rejects_unrewritten_model_and_serves_nonstream_title(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.post({"model": "fixture-model", "stream": True, "messages": []})
        self.assertEqual(raised.exception.code, 422)

        title = json.loads(
            self.post(
                {
                    "model": "fixture-wire-model",
                    "stream": False,
                    "messages": [{"role": "user", "content": "Make a title"}],
                }
            )
        )
        self.assertEqual(title["choices"][0]["message"]["content"], "JobBench local OpenCode fixture")

    def test_inspection_command_opens_supported_formats_and_records_only_counts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task = root / "task"
            output = root / "output"
            task.mkdir()
            (task / "TASK_INSTRUCTIONS.txt").write_text("Inspect these fixture files.\n")
            (task / "sample.csv").write_text("name,value\nalpha,7\n")

            from openpyxl import Workbook
            from PIL import Image

            workbook = Workbook()
            workbook.active.append(["name", "value"])
            workbook.active.append(["alpha", 7])
            workbook.save(task / "sample.xlsx")
            image = Image.new("RGB", (6, 4), "white")
            image.save(task / "sample.png")
            image.save(task / "sample.pdf", "PDF")
            with zipfile.ZipFile(task / "sample.docx", "w") as archive:
                archive.writestr(
                    "[Content_Types].xml",
                    """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""",
                )
                archive.writestr(
                    "_rels/.rels",
                    """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""",
                )
                archive.writestr(
                    "word/document.xml",
                    """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>fixture paragraph</w:t></w:r></w:p><w:sectPr/></w:body>
</w:document>""",
                )
            connection = sqlite3.connect(task / "sample.db")
            try:
                connection.execute("CREATE TABLE sample(value INTEGER)")
                connection.execute("INSERT INTO sample VALUES (7)")
                connection.commit()
            finally:
                connection.close()

            env = dict(os.environ)
            env["PATH"] = f"{Path(sys.executable).parent}:{env['PATH']}"
            subprocess.run(
                build_inspection_command(task, output),
                shell=True,
                check=True,
                env=env,
                capture_output=True,
                text=True,
            )

            report = json.loads((output / "smoke.json").read_text())
            checks = {Path(item["path"]).suffix: item for item in report["files"]}
            self.assertEqual(
                checks[".csv"]["counts"],
                {"first_row_cells": 2, "rows_sampled": 1},
            )
            self.assertEqual(
                checks[".xlsx"]["counts"],
                {"first_row_cells": 2, "first_sheet_rows": 2, "sheets": 1},
            )
            self.assertGreaterEqual(checks[".pdf"]["counts"]["pages"], 1)
            self.assertGreaterEqual(checks[".docx"]["counts"]["paragraphs"], 1)
            self.assertEqual(checks[".png"]["counts"], {"height": 4, "width": 6})
            self.assertEqual(checks[".db"]["counts"], {"tables": 1})
            serialized = json.dumps(report)
            self.assertNotIn("alpha", serialized)
            self.assertNotIn("Judge", serialized)


if __name__ == "__main__":
    unittest.main()
