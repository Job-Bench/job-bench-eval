"""Exercise judge orchestration with temporary tasks and an offline judge process."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FAKE_JUDGE = r'''
import argparse
import json
import os
from pathlib import Path
import sys

implementation = {"sha256": "current-offline-judge"}
if sys.argv[1:] == ["--print-implementation"]:
    print(json.dumps(implementation))
    raise SystemExit(0)

parser = argparse.ArgumentParser()
for name in ("output-dir", "rubrics-file", "details-file", "judge-model",
             "api-base", "api-key", "max-workers", "max-retries",
             "timeout-per-rubric", "evaluated-model", "lock-file",
             "detail-log-dir", "detail-log-prefix"):
    parser.add_argument("--" + name, required=True)
args = parser.parse_args()
with Path(os.environ["JUDGE_TEST_EVENTS"]).open("a") as handle:
    handle.write(json.dumps(vars(args)) + "\n")
if Path(args.output_dir).name == "a-failing" and args.judge_model != "good-judge":
    print("Offline fixture: evidence extraction failed", file=sys.stderr)
    raise SystemExit(2)

rubrics = json.loads(Path(args.rubrics_file).read_text())["rubrics"]
report = {
    "evaluated_model": args.evaluated_model,
    "judge_model": args.judge_model,
    "judge_implementation": implementation,
    "total_score": 1,
    "max_score": 1,
    "passed_count": 1,
    "total_count": len(rubrics),
    "pass_rate": "100%",
    "rubrics": [{"index": 0, "result": {"passed": True, "score": 1}}],
}
Path(args.details_file).write_text(json.dumps(report))
'''


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "requires bash and jq")
class JudgeRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        evaluation = self.root / "eval"
        evaluation.mkdir()
        self.runner = evaluation / "run_judge.sh"
        shutil.copyfile(ROOT / "eval" / "run_judge.sh", self.runner)
        (evaluation / "judge.py").write_text(FAKE_JUDGE)
        (evaluation / "check_dataset_freshness.sh").write_text("#!/bin/bash\nexit 0\n")
        self.tasks = self.root / "dataset"
        self.events = self.root / "judge-events.jsonl"
        self.env = {
            **os.environ,
            "TARGET_DIR": str(self.tasks),
            "JUDGE_PYTHON": sys.executable,
            "JUDGE_MODEL": "provider/test-judge.1",
            "JUDGE_MODELS": "provider/test-judge.1",
            "JUDGE_RUN_LABEL": "",
            "EVAL_MODEL": "",
            "JUDGE_API_BASE": "http://unused.invalid",
            "JUDGE_API_KEY": "offline-fixture",
            "JUDGE_ALT_API_BASE": "http://unused.invalid",
            "JUDGE_ALT_API_KEY": "offline-fixture",
            "JUDGE_ALT_MODELS": "",
            "JUDGE_TEST_EVENTS": str(self.events),
        }

    def task(self, number, models=("model",)):
        task = self.tasks / "occupation" / f"task{number}"
        for model in models:
            output = task / "model_output" / model
            output.mkdir(parents=True)
            (output / "answer.txt").write_text("Saved evidence")
        (task / "RUBRICS.json").write_text(json.dumps({
            "rubrics": [{"rubric": "Answer is correct", "weight": 1, "criterion": ["Correct answer"]}],
        }))
        return task

    def historical_report(self, task):
        report = task / "eval_result" / "eval_model" / "test-judge-1_judge.json"
        report.parent.mkdir(parents=True)
        original = json.dumps({
            "evaluated_model": "model", "judge_model": "provider/test-judge.1",
            "total_score": 1, "max_score": 1, "passed_count": 1,
            "total_count": 1, "pass_rate": "100%",
            "rubrics": [{"index": 0, "result": {"passed": True, "score": 1}}],
        }) + "\n"
        report.write_text(original)
        return report, original

    def invoke(self, *command, environment=None):
        return subprocess.run(
            command or ("bash", str(self.runner)),
            env={**self.env, **(environment or {})},
            text=True, capture_output=True, timeout=20,
        )

    def invocations(self):
        if not self.events.exists():
            return []
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def test_default_run_warns_and_preserves_completed_historical_scores(self):
        historical, original = self.historical_report(self.task(1))

        result = self.invoke()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(historical.read_text(), original)
        self.assertIn("JUDGE_RUN_LABEL", result.stdout + result.stderr)
        self.assertIn("[WARN]", result.stdout + result.stderr)
        self.assertIn("[SKIP] Already judged", result.stdout)
        self.assertEqual(self.invocations(), [])

    def test_fresh_label_writes_separate_report_and_log_paths(self):
        task = self.task(1)
        historical, original = self.historical_report(task)

        result = self.invoke(environment={"JUDGE_RUN_LABEL": "rich-v2.1"})

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(historical.read_text(), original)
        fresh = task / "eval_result" / "eval_model" / "test-judge-1_rich-v2.1_judge.json"
        self.assertTrue(fresh.is_file())
        calls = self.invocations()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["details_file"], str(fresh))
        self.assertTrue(calls[0]["detail_log_prefix"].endswith("test-judge-1_rich-v2.1"))
        self.assertEqual(Path(calls[0]["lock_file"]).name, ".test-judge-1_rich-v2.1_judge.lock")

    def test_task_returns_failure_after_processing_remaining_models(self):
        task = self.task(1, ("a-failing", "z-success"))

        result = self.invoke("bash", "-c", 'source "$1"\njudge_task "$2"',
                             "test", str(self.runner), str(task))

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Judge process failed with exit code 2", result.stdout)
        self.assertEqual([call["evaluated_model"] for call in self.invocations()],
                         ["a-failing", "z-success"])
        self.assertTrue((task / "eval_result/eval_z-success/test-judge-1_judge.json").is_file())

    def test_judge_model_returns_task_failure_through_tee_and_runs_later_tasks(self):
        self.task(1, ("a-failing",))
        later = self.task(2)

        result = self.invoke("bash", "-c", 'source "$1"\nrun_with_judge_model "$JUDGE_MODEL"',
                             "test", str(self.runner))

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.invocations()), 2)
        self.assertTrue((later / "eval_result/eval_model/test-judge-1_judge.json").is_file())

    def test_cli_counts_failed_judge_models_and_keeps_processing(self):
        self.task(1, ("a-failing",))
        later = self.task(2)

        result = self.invoke(environment={"JUDGE_MODELS": "provider/test-judge.1 good-judge"})

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Failed: 1", result.stdout)
        self.assertEqual(len(self.invocations()), 4)
        for name in ("test-judge-1_judge.json", "good-judge_judge.json"):
            self.assertTrue((later / "eval_result/eval_model" / name).is_file())

    def test_unsafe_label_is_rejected_before_judging_or_writing_reports(self):
        task = self.task(1)

        for label in ("../old", "two labels", "bad;label"):
            with self.subTest(label=label):
                result = self.invoke(environment={"JUDGE_RUN_LABEL": label})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("JUDGE_RUN_LABEL", result.stderr)
                self.assertEqual(self.invocations(), [])
                self.assertFalse((task / "eval_result").exists())


if __name__ == "__main__":
    unittest.main()
