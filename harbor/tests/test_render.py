from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


HARBOR_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARBOR_ROOT))

from jobbench_harbor.render import render_task


class RenderTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        task_folder = self.source / "task_folder"
        task_folder.mkdir(parents=True)
        self.prompt = "Create a report.\nPreserve this raw prompt exactly.\n"
        (task_folder / "TASK_INSTRUCTIONS.txt").write_bytes(self.prompt.encode())
        (task_folder / "input.bin").write_bytes(b"\x00\xffsource-bytes\r\n")
        (task_folder / "nested").mkdir()
        (task_folder / "nested" / "input.csv").write_bytes(b"a,b\r\n1,2\r\n")
        self.rubric_bytes = json.dumps(
            {
                "rubrics": [
                    {
                        "rubric": "The report is complete",
                        "weight": 7,
                        "criterion": ["report exists"],
                    }
                ]
            },
            indent=2,
        ).encode()
        (self.source / "RUBRICS.json").write_bytes(self.rubric_bytes)
        (self.source / "task_card.md").write_bytes(b"SECRET TASK CARD")
        (self.source / "files_required_to_search").mkdir()
        (self.source / "files_required_to_search" / "answer.pdf").write_bytes(
            b"SECRET SEARCH REFERENCE"
        )
        (self.source / "model_output").mkdir()
        (self.source / "model_output" / "answer.txt").write_bytes(
            b"SECRET HISTORICAL OUTPUT"
        )
        self.judge = self.base / "judge.py"
        self.judge_bytes = b"#!/usr/bin/env python3\nprint('original judge')\n"
        self.judge.write_bytes(self.judge_bytes)
        self.destination = self.base / "rendered"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def render(self, *, split: str = "easy") -> dict:
        return render_task(
            self.source,
            self.destination,
            task_id=f"jobbench-{split}-analysts-task2",
            split=split,
            occupation="analysts",
            task_number="task2",
            repo_id="JobBench/job-bench",
            revision="a" * 40,
            judge_path=self.judge,
            search_files=["files_required_to_search/answer.pdf"],
        )

    def test_agent_image_contains_exact_task_folder_bytes_only(self) -> None:
        self.render()

        copied = self.destination / "environment" / "task_folder"
        self.assertEqual(
            {
                path.relative_to(copied).as_posix(): path.read_bytes()
                for path in copied.rglob("*")
                if path.is_file()
            },
            {
                "TASK_INSTRUCTIONS.txt": self.prompt.encode(),
                "input.bin": b"\x00\xffsource-bytes\r\n",
                "nested/input.csv": b"a,b\r\n1,2\r\n",
            },
        )
        environment_bytes = b"\n".join(
            path.read_bytes()
            for path in (self.destination / "environment").rglob("*")
            if path.is_file()
        )
        for secret in (
            b"SECRET TASK CARD",
            b"SECRET SEARCH REFERENCE",
            b"SECRET HISTORICAL OUTPUT",
            self.rubric_bytes,
            self.judge_bytes,
        ):
            self.assertNotIn(secret, environment_bytes)

    def test_instruction_keeps_runner_envelope_paths_and_raw_prompt(self) -> None:
        self.render(split="main")

        instruction = (self.destination / "instruction.md").read_text()
        self.assertIn("=== TASK FOLDER ===\n/workspace/task_folder", instruction)
        self.assertIn("=== OUTPUT DIRECTORY ===\n/workspace/output", instruction)
        self.assertIn("Read the TASK_INSTRUCTIONS.txt file", instruction)
        self.assertIn("search online for new reference files", instruction)
        self.assertIn(
            "save the final modified copy in /workspace/output",
            instruction,
        )
        self.assertEqual(
            (
                self.destination
                / "environment"
                / "task_folder"
                / "TASK_INSTRUCTIONS.txt"
            ).read_text(),
            self.prompt,
        )

    def test_schema_metadata_public_network_artifact_and_separate_verifier(self) -> None:
        self.render(split="easy")

        raw = tomllib.loads((self.destination / "task.toml").read_text())
        self.assertEqual(raw["schema_version"], "1.4")
        self.assertEqual(raw["artifacts"], ["/workspace/output"])
        self.assertEqual(raw["metadata"]["id"], "jobbench-easy-analysts-task2")
        self.assertEqual(raw["metadata"]["split"], "easy")
        self.assertEqual(raw["metadata"]["occupation"], "analysts")
        self.assertEqual(raw["metadata"]["task_number"], "task2")
        self.assertEqual(raw["environment"]["network_mode"], "public")
        self.assertEqual(raw["environment"]["workdir"], "/workspace")
        self.assertEqual(raw["verifier"]["environment_mode"], "separate")
        self.assertEqual(raw["verifier"]["network_mode"], "public")
        self.assertEqual(raw["verifier"]["environment"]["network_mode"], "public")
        self.assertEqual(raw["verifier"]["env"]["JUDGE_MODEL"], "${JUDGE_MODEL:-grok-4.3}")
        self.assertEqual(raw["verifier"]["env"]["JUDGE_API_BASE"], "${JUDGE_API_BASE:-https://api.x.ai/v1}")
        self.assertEqual(raw["verifier"]["env"]["JUDGE_API_KEY"], "${JUDGE_API_KEY:-}")
        self.assertNotIn("env", raw["agent"])

        from harbor.models.task.task import Task

        parsed = Task(self.destination)
        self.assertEqual(parsed.config.schema_version, "1.4")
        self.assertEqual(parsed.config.environment.network_mode.value, "public")

    def test_tests_image_owns_byte_identical_judge_rubric_and_wrapper(self) -> None:
        self.render()

        tests = self.destination / "tests"
        self.assertEqual((tests / "judge.py").read_bytes(), self.judge_bytes)
        self.assertEqual((tests / "RUBRICS.json").read_bytes(), self.rubric_bytes)
        self.assertTrue((tests / "verify.py").is_file())
        self.assertTrue((tests / "test.sh").is_file())
        self.assertTrue((tests / "Dockerfile").is_file())
        self.assertNotIn("RUBRICS", (tests / "Dockerfile").read_text())
        self.assertNotIn("judge.py", (self.destination / "environment" / "Dockerfile").read_text())

    def test_provenance_hashes_source_files_without_exposing_search_bytes(self) -> None:
        record = self.render()

        self.assertEqual(record["id"], "jobbench-easy-analysts-task2")
        self.assertEqual(record["split"], "easy")
        self.assertEqual(record["occupation"], "analysts")
        self.assertEqual(record["task_number"], "task2")
        self.assertEqual(record["repo_id"], "JobBench/job-bench")
        self.assertEqual(record["revision"], "a" * 40)
        self.assertEqual(
            record["search_files"], ["files_required_to_search/answer.pdf"]
        )
        files = {item["path"]: item for item in record["source_files"]}
        self.assertEqual(
            set(files),
            {
                "RUBRICS.json",
                "task_card.md",
                "task_folder/TASK_INSTRUCTIONS.txt",
                "task_folder/input.bin",
                "task_folder/nested/input.csv",
            },
        )
        self.assertEqual(
            files["task_folder/input.bin"]["sha256"],
            hashlib.sha256(b"\x00\xffsource-bytes\r\n").hexdigest(),
        )
        all_rendered = b"\n".join(
            path.read_bytes()
            for path in self.destination.rglob("*")
            if path.is_file()
        )
        self.assertNotIn(b"SECRET SEARCH REFERENCE", all_rendered)
        self.assertNotIn(b"SECRET TASK CARD", all_rendered)
        self.assertNotIn(b"SECRET HISTORICAL OUTPUT", all_rendered)

    def test_provenance_rejects_symlinked_hidden_metadata(self) -> None:
        outside = self.base / "outside-card.md"
        outside.write_bytes(b"OUTSIDE SECRET")
        (self.source / "task_card.md").unlink()
        (self.source / "task_card.md").symlink_to(outside)

        with self.assertRaisesRegex(ValueError, "task card"):
            self.render()


if __name__ == "__main__":
    unittest.main()
