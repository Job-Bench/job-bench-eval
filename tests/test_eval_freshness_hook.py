"""Exercise ordinary entry points without launching any real agent or judge."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNERS = (
    "run_benchmark_opencode.sh",
    "run_benchmark_claude_code_cli.sh",
    "run_benchmark_codex_cli.sh",
    "run_judge.sh",
)


class OrdinaryFreshnessHookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "eval").mkdir()
        self.events = self.root / "events"
        self.tasks = self.root / "custom-tasks"
        (self.tasks / "profession/task1/task_folder").mkdir(parents=True)
        (self.root / "opencode/packages/opencode").mkdir(parents=True)
        self.env = dict(
            os.environ,
            EVENTS=str(self.events),
            TASKS_BASE_DIR=str(self.tasks),
            TARGET_DIR=str(self.tasks),
            OPENCODE_DIR=str(self.root / "opencode"),
            BENCHMARK_MODELS="provider/model|first provider/another|second",
            JUDGE_PYTHON=sys.executable,
            JUDGE_MODELS="fake-judge",
            OPENAI_BASE_URL="",
            JOBBENCH_SKIP_HF_CHECK="0",
        )

    def invoke_runner(self, name, checker_status):
        self.events.unlink(missing_ok=True)
        shutil.copy2(ROOT / "eval" / name, self.root / "eval" / name)
        (self.root / "eval/check_dataset_freshness.sh").write_text(
            '#!/bin/bash\nprintf "CHECK %s\\n" "$1" >> "$EVENTS"\n'
            'if [[ -n "${CHECK_CWD_FILE:-}" ]]; then pwd > "$CHECK_CWD_FILE"; fi\n'
            f'exit {checker_status}\n'
        )
        script = '''
source "$1"
configure_opencode_provider_timeouts() { :; }
npx() { :; }
find_task_dirs() { printf '%s\\n' "$TASKS_BASE_DIR/profession/task1/task_folder"; }
run_task() { echo RUN >> "$EVENTS"; }
run_model_tasks() { echo RUN >> "$EVENTS"; }
run_with_judge_model() { echo RUN >> "$EVENTS"; }
main
'''
        result = subprocess.run(
            ["bash", "-c", script, "test", str(self.root / "eval" / name)],
            env=self.env, text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.events.read_text().splitlines()

    def test_each_entry_point_checks_actual_task_root_once_before_execution(self):
        for name in RUNNERS:
            with self.subTest(runner=name):
                events = self.invoke_runner(name, 0)
                self.assertEqual(events[0], f"CHECK {self.tasks}")
                self.assertEqual(sum(event.startswith("CHECK") for event in events), 1)
                self.assertIn("RUN", events[1:])

    def test_checker_failure_does_not_prevent_agents_or_judge(self):
        for name in RUNNERS:
            with self.subTest(runner=name):
                events = self.invoke_runner(name, 23)
                self.assertEqual(events[0], f"CHECK {self.tasks}")
                self.assertIn("RUN", events[1:])

    def test_relative_task_root_uses_the_runners_task_discovery_directory(self):
        cwd_record = self.root / "check-cwd"
        self.env.update(TASKS_BASE_DIR="relative-tasks", CHECK_CWD_FILE=str(cwd_record))
        for name in RUNNERS[:3]:
            with self.subTest(runner=name):
                self.invoke_runner(name, 0)
                expected = self.root / ("opencode" if "opencode" in name else "eval")
                self.assertEqual(cwd_record.read_text().strip(), str(expected))

    def prepare_real_bridge(self):
        bridge = ROOT / "eval/check_dataset_freshness.sh"
        self.assertTrue(bridge.is_file(), "ordinary freshness bridge is not implemented")
        shutil.copy2(bridge, self.root / "eval/check_dataset_freshness.sh")
        (self.root / "scripts").mkdir()
        (self.root / ".venv/bin").mkdir(parents=True)
        (self.root / ".venv/bin/python").symlink_to(sys.executable)
        (self.root / "scripts/check_dataset_freshness.py").write_text(
            'import json, os, sys\nfrom pathlib import Path\n'
            'Path(os.environ["EVENTS"]).write_text(json.dumps(sys.argv[1:]))\n'
            'print("advisory", file=sys.stderr)\n'
            'raise SystemExit(7)\n'
        )

    def test_bridge_forwards_path_with_spaces_and_preserves_nonfatal_advisory(self):
        self.prepare_real_bridge()
        selected = str(self.root / "a custom subset")
        result = subprocess.run(
            ["bash", str(self.root / "eval/check_dataset_freshness.sh"), selected],
            env=self.env, text=True, capture_output=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.events.read_text()), ["--dataset-root", selected])
        self.assertIn("advisory", result.stderr)
        self.assertIn("[WARN]", result.stderr)

    def test_skip_setting_does_not_start_checker(self):
        self.prepare_real_bridge()
        result = subprocess.run(
            ["bash", str(self.root / "eval/check_dataset_freshness.sh"), str(self.tasks)],
            env={**self.env, "JOBBENCH_SKIP_HF_CHECK": "1"},
            text=True, capture_output=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout + result.stderr, "")
        self.assertFalse(self.events.exists())


if __name__ == "__main__":
    unittest.main()
