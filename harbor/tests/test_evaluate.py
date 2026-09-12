import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

import yaml

HARBOR_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARBOR_ROOT))

from jobbench_harbor import evaluate


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "harbor"
        self.root.mkdir()
        self.invocation_path = self.root / "invocation.json"
        self.stub_path = self.root / "fake-harbor"
        self.stub_path.write_text(
            """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import signal
import sys

args = sys.argv[1:]
Path(os.environ["FAKE_HARBOR_INVOCATION"]).write_text(json.dumps(args))
if os.environ.get("FAKE_HARBOR_EXIT_EARLY"):
    raise SystemExit(int(os.environ["FAKE_HARBOR_EXIT_EARLY"]))
config_path = Path(args[args.index("--config") + 1])
config = json.loads(config_path.read_text())
job_dir = Path(config["jobs_dir"]) / config["job_name"]
job_dir.mkdir(parents=True)
(job_dir / "config.json").write_text(json.dumps(config))
if os.environ.get("FAKE_HARBOR_INTERRUPT_PARENT"):
    os.kill(os.getppid(), signal.SIGINT)
    raise SystemExit(130)
"""
        )
        self.stub_path.chmod(self.stub_path.stat().st_mode | stat.S_IXUSR)
        self.environment_patch = mock.patch.dict(
            os.environ,
            {
                "FAKE_HARBOR_INVOCATION": str(self.invocation_path),
                "JOBBENCH_SKIP_HF_CHECK": "1",
            },
            clear=False,
        )
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)
        self.generation = self._publish_generation(
            "g1",
            [
                {
                    "id": "main--accountant--task1",
                    "split": "main",
                    "occupation": "accountant",
                    "task_number": 1,
                },
                {
                    "id": "easy--accountant--task1",
                    "split": "easy",
                    "occupation": "accountant",
                    "task_number": 1,
                },
                {
                    "id": "main--writer--task2",
                    "split": "main",
                    "occupation": "writer",
                    "task_number": 2,
                },
            ],
        )

    def _publish_generation(self, name, tasks):
        generation = self.root / ".generated" / name
        tasks_dir = generation / "tasks"
        tasks_dir.mkdir(parents=True)
        for task in tasks:
            (tasks_dir / task["id"]).mkdir()
        manifest = {
            "repo_id": "JobBench/job-bench",
            "revision": "0123456789abcdef",
            "judge_sha256": "a" * 64,
            "tasks": tasks,
        }
        (generation / "manifest.json").write_text(json.dumps(manifest))
        current = self.root / "current"
        if current.is_symlink():
            current.unlink()
        current.symlink_to(generation.relative_to(self.root))
        return generation

    def _run(self, arguments, *, environment=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, environment or {}, clear=False):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                return_code = evaluate.main(
                    arguments,
                    root=self.root,
                    harbor_executable=self.stub_path,
                )
        return return_code, stdout.getvalue(), stderr.getvalue()

    def _native_config(self):
        invocation = json.loads(self.invocation_path.read_text())
        self.assertEqual(invocation[0], "run")
        config_path = Path(invocation[invocation.index("--config") + 1])
        # The wrapper removes its private temporary config after Harbor exits;
        # Harbor's recorded config is the stable, effective native config.
        job_dirs = list((self.root / "jobs").iterdir())
        self.assertEqual(len(job_dirs), 1)
        return invocation, json.loads((job_dirs[0] / "config.json").read_text())

    def _install_freshness_checker(self):
        checker = self.root.parent / "scripts" / "check_dataset_freshness.py"
        checker.parent.mkdir()
        checker.write_text(
            """import json
import os
from pathlib import Path
import sys
import time

manifest = Path(sys.argv[sys.argv.index("--manifest") + 1])
with Path(os.environ["FAKE_HF_INVOCATIONS"]).open("a") as output:
    output.write(json.dumps({
        "args": sys.argv[1:],
        "manifest": json.loads(manifest.read_text()),
        "python": sys.executable,
        "native_started": Path(os.environ["FAKE_HARBOR_INVOCATION"]).exists(),
    }) + "\\n")
if os.environ.get("FAKE_HF_SLEEP"):
    time.sleep(30)
if os.environ.get("FAKE_HF_ERROR"):
    raise RuntimeError("fixture checker failure")
print("WARNING: JobBench dataset is stale; a newer HF revision is available.", file=sys.stderr)
raise SystemExit(int(os.environ.get("FAKE_HF_EXIT", "0")))
"""
        )
        self.freshness_invocations = self.root / "freshness.jsonl"
        environment_patch = mock.patch.dict(
            os.environ,
            {
                "JOBBENCH_SKIP_HF_CHECK": "0",
                "FAKE_HF_INVOCATIONS": str(self.freshness_invocations),
            },
        )
        environment_patch.start()
        self.addCleanup(environment_patch.stop)

    def test_freshness_checks_resolved_manifest_once_before_native_when_current_switches(self):
        self._install_freshness_checker()
        original_resolve = evaluate._resolve_generation

        def resolve_then_publish(*args, **kwargs):
            resolved = original_resolve(*args, **kwargs)
            self._publish_generation(
                "g2", [{"id": "main--new--task3", "split": "main"}]
            )
            return resolved

        with mock.patch.object(
            evaluate, "_resolve_generation", side_effect=resolve_then_publish
        ):
            result, _stdout, stderr = self._run(["--job-name", "pinned-job"])

        self.assertEqual(result, 0, stderr)
        self.assertIn("stale", stderr)
        checks = [
            json.loads(line)
            for line in self.freshness_invocations.read_text().splitlines()
        ]
        self.assertEqual(len(checks), 1)
        self.assertEqual(
            checks[0]["args"], ["--manifest", str(self.generation / "manifest.json")]
        )
        self.assertEqual(checks[0]["python"], sys.executable)
        self.assertFalse(checks[0]["native_started"])
        self.assertEqual(checks[0]["manifest"]["tasks"][0]["id"], "main--accountant--task1")
        self.assertEqual((self.root / "current").resolve().name, "g2")
        _invocation, config = self._native_config()
        self.assertEqual(
            [task["path"] for task in config["tasks"]],
            [
                str(self.generation / "tasks/main--accountant--task1"),
                str(self.generation / "tasks/main--writer--task2"),
            ],
        )
        provenance = json.loads(
            (self.root / "jobs/pinned-job/jobbench_provenance.json").read_text()
        )
        self.assertEqual(provenance["generation_id"], "g1")
        self.assertEqual(
            provenance["source"],
            {"repo_id": "JobBench/job-bench", "revision": "0123456789abcdef"},
        )

    def test_freshness_warning_and_checker_exit_do_not_replace_native_exit_status(self):
        self._install_freshness_checker()

        result, _stdout, stderr = self._run(
            ["--future-native-option", "value with spaces"],
            environment={"FAKE_HF_EXIT": "9", "FAKE_HARBOR_EXIT_EARLY": "37"},
        )

        self.assertEqual(result, 37)
        self.assertIn("stale", stderr)
        invocation = json.loads(self.invocation_path.read_text())
        self.assertEqual(invocation[-2:], ["--future-native-option", "value with spaces"])

    def test_freshness_python_error_is_advisory_and_native_still_runs(self):
        self._install_freshness_checker()

        result, _stdout, stderr = self._run([], environment={"FAKE_HF_ERROR": "1"})

        self.assertEqual(result, 0, stderr)
        self.assertIn("freshness", stderr.lower())
        self.assertIn("unknown", stderr.lower())
        self.assertTrue(self.invocation_path.exists())

    def test_freshness_timeout_is_bounded_and_native_still_runs(self):
        self._install_freshness_checker()
        started = time.monotonic()
        with mock.patch.object(evaluate, "_FRESHNESS_CHECK_TIMEOUT_SECONDS", 0.2):
            result, _stdout, stderr = self._run([], environment={"FAKE_HF_SLEEP": "1"})

        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(result, 0, stderr)
        self.assertIn("freshness", stderr.lower())
        self.assertIn("unknown", stderr.lower())
        self.assertTrue(self.invocation_path.exists())

    def test_missing_freshness_checker_is_advisory_and_native_still_runs(self):
        result, _stdout, stderr = self._run([], environment={"JOBBENCH_SKIP_HF_CHECK": "0"})

        self.assertEqual(result, 0, stderr)
        self.assertIn("freshness", stderr.lower())
        self.assertIn("unknown", stderr.lower())
        self.assertTrue(self.invocation_path.exists())

    def test_dry_run_checks_freshness_without_starting_native_or_creating_jobs(self):
        self._install_freshness_checker()

        result, stdout, stderr = self._run(["--dry-run"])

        self.assertEqual(result, 0, stderr)
        self.assertIn("stale", stderr)
        self.assertIn("2 task(s)", stdout)
        self.assertEqual(len(self.freshness_invocations.read_text().splitlines()), 1)
        self.assertFalse(self.invocation_path.exists())
        self.assertFalse((self.root / "jobs").exists())

    def test_skip_freshness_dry_run_spawns_no_subprocess(self):
        with mock.patch.object(
            evaluate.subprocess, "run", side_effect=AssertionError("unexpected subprocess")
        ):
            result, _stdout, stderr = self._run(["--dry-run"])

        self.assertEqual(result, 0, stderr)
        self.assertEqual(stderr, "")

    def test_help_does_not_check_freshness_or_start_native(self):
        self._install_freshness_checker()
        with mock.patch.object(
            evaluate.subprocess, "run", side_effect=AssertionError("unexpected subprocess")
        ):
            with self.assertRaises(SystemExit) as raised:
                self._run(["--help"])

        self.assertEqual(raised.exception.code, 0)
        self.assertFalse(self.freshness_invocations.exists())
        self.assertFalse(self.invocation_path.exists())

    def test_print_config_skips_freshness_and_forwards_to_native(self):
        self._install_freshness_checker()

        result, _stdout, stderr = self._run(["--print-config"])

        self.assertEqual(result, 0, stderr)
        self.assertFalse(self.freshness_invocations.exists())
        invocation = json.loads(self.invocation_path.read_text())
        self.assertIn("--print-config", invocation)

    def test_invalid_selection_does_not_check_freshness(self):
        self._install_freshness_checker()

        result, _stdout, stderr = self._run(["--n-tasks", "0"])

        self.assertEqual(result, 2, stderr)
        self.assertFalse(self.freshness_invocations.exists())
        self.assertFalse(self.invocation_path.exists())

    def test_default_split_runs_only_main_manifest_tasks_from_resolved_generation(self):
        result, _stdout, stderr = self._run(["-a", "oracle"])

        self.assertEqual(result, 0, stderr)
        invocation, config = self._native_config()
        self.assertEqual(invocation[-2:], ["-a", "oracle"])
        self.assertEqual(
            config["tasks"],
            [
                {"path": str(self.generation / "tasks/main--accountant--task1")},
                {"path": str(self.generation / "tasks/main--writer--task2")},
            ],
        )
        self.assertEqual(config["datasets"], [])

    def test_easy_and_all_split_selection_uses_manifest_records(self):
        result, _stdout, stderr = self._run(["--split", "easy"])
        self.assertEqual(result, 0, stderr)
        _invocation, config = self._native_config()
        self.assertEqual(
            config["tasks"],
            [{"path": str(self.generation / "tasks/easy--accountant--task1")}],
        )

        # Use a separate output directory because every invocation is a new job.
        other_jobs = self.root / "all-jobs"
        result, _stdout, stderr = self._run(
            ["--split=all", "--jobs-dir", str(other_jobs)]
        )
        self.assertEqual(result, 0, stderr)
        job_dir = next(other_jobs.iterdir())
        config = json.loads((job_dir / "config.json").read_text())
        self.assertEqual(
            [Path(task["path"]).name for task in config["tasks"]],
            [
                "main--accountant--task1",
                "easy--accountant--task1",
                "main--writer--task2",
            ],
        )

    def test_manifest_task_filters_are_owned_by_wrapper(self):
        result, _stdout, stderr = self._run(
            ["--split", "all", "-i", "main--writer*", "--n-tasks", "1", "-a", "oracle"]
        )

        self.assertEqual(result, 0, stderr)
        invocation, config = self._native_config()
        self.assertEqual(invocation[-2:], ["-a", "oracle"])
        self.assertEqual(
            config["tasks"],
            [{"path": str(self.generation / "tasks/main--writer--task2")}],
        )

    def test_task_name_alias_and_exclusion_filter_manifest_ids(self):
        result, _stdout, stderr = self._run(
            [
                "--split=all",
                "--task-name",
                "*accountant*",
                "--exclude-task-name",
                "easy--*",
            ]
        )

        self.assertEqual(result, 0, stderr)
        invocation, config = self._native_config()
        self.assertEqual(len(invocation), 3)
        self.assertEqual(
            config["tasks"],
            [{"path": str(self.generation / "tasks/main--accountant--task1")}],
        )

    def test_native_yaml_and_cli_agent_overrides_are_preserved(self):
        config_path = self.root / "agent.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "n_concurrent_trials": 3,
                    "agents": [
                        {
                            "name": "from-yaml",
                            "model_name": "yaml-model",
                            "kwargs": {"max_output_chars": 64000},
                        }
                    ],
                }
            )
        )
        passthrough = [
            "--agent",
            "package.custom:Agent",
            "-m",
            "provider/model with spaces",
            "--ak",
            "max_output_chars=120000",
            "--ak=arbitrary={\"nested\":true}",
            "--n-concurrent",
            "7",
        ]

        result, _stdout, stderr = self._run(
            ["--config", str(config_path), *passthrough]
        )

        self.assertEqual(result, 0, stderr)
        invocation, config = self._native_config()
        config_index = invocation.index("--config")
        self.assertEqual(invocation[config_index + 2 :], passthrough)
        self.assertEqual(config["n_concurrent_trials"], 3)
        self.assertEqual(config["agents"][0]["name"], "from-yaml")
        self.assertEqual(config["agents"][0]["kwargs"]["max_output_chars"], 64000)

    def test_model_only_cli_override_updates_single_yaml_agent(self):
        config_path = self.root / "single-agent.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "agents": [
                        {
                            "name": "terminus-2",
                            "model_name": "yaml-model",
                            "kwargs": {
                                "llm_call_kwargs": {"max_tokens": 8192},
                                "max_turns": 50,
                            },
                        }
                    ]
                }
            )
        )

        result, _stdout, stderr = self._run(
            [
                "--config",
                str(config_path),
                "-m",
                "provider/first",
                "--model=provider/second",
                "--ak",
                "max_turns=73",
            ]
        )

        self.assertEqual(result, 0, stderr)
        invocation, config = self._native_config()
        self.assertEqual(invocation[-2:], ["--ak", "max_turns=73"])
        self.assertEqual(
            [agent["model_name"] for agent in config["agents"]],
            ["provider/first", "provider/second"],
        )
        self.assertEqual(
            [agent["kwargs"] for agent in config["agents"]],
            [
                {"llm_call_kwargs": {"max_tokens": 8192}, "max_turns": 50},
                {"llm_call_kwargs": {"max_tokens": 8192}, "max_turns": 50},
            ],
        )

    def test_model_only_override_rejects_ambiguous_yaml_agents(self):
        config_path = self.root / "multiple-agents.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "agents": [
                        {"name": "terminus-2", "model_name": "one"},
                        {"name": "codex", "model_name": "two"},
                    ]
                }
            )
        )

        result, _stdout, stderr = self._run(
            ["--config", str(config_path), "-m", "provider/override"]
        )

        self.assertEqual(result, 2)
        self.assertIn("exactly one agent", stderr)
        self.assertFalse(self.invocation_path.exists())

    def test_provenance_is_written_to_actual_custom_job_and_contains_no_secrets(self):
        jobs_dir = self.root / "custom-results"
        config_path = self.root / "secret-config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "agents": [
                        {
                            "name": "custom.agent:Agent",
                            "env": {"AGENT_TOKEN": "config-secret"},
                        }
                    ]
                }
            )
        )

        result, _stdout, stderr = self._run(
            [
                "-c",
                str(config_path),
                "--jobs-dir",
                str(jobs_dir),
                "--job-name",
                "named-job",
                "--ak",
                "token=cli-secret",
            ],
            environment={"JUDGE_API_KEY": "environment-secret"},
        )

        self.assertEqual(result, 0, stderr)
        provenance_path = jobs_dir / "named-job" / "jobbench_provenance.json"
        provenance_text = provenance_path.read_text()
        provenance = json.loads(provenance_text)
        self.assertNotIn("config-secret", provenance_text)
        self.assertNotIn("cli-secret", provenance_text)
        self.assertNotIn("environment-secret", provenance_text)
        self.assertEqual(
            provenance["source"],
            {"repo_id": "JobBench/job-bench", "revision": "0123456789abcdef"},
        )
        self.assertEqual(provenance["judge_sha256"], "a" * 64)
        self.assertEqual(provenance["generation_id"], "g1")
        self.assertEqual(provenance["split"], "main")
        self.assertEqual(
            [task["id"] for task in provenance["tasks"]],
            ["main--accountant--task1", "main--writer--task2"],
        )
        self.assertEqual(provenance["harbor"], {"version": "0.22.0", "job_config": "config.json"})
        self.assertEqual(provenance["adapter"]["version"], "0.1.0")
        self.assertEqual(len(provenance["adapter"]["sha256"]), 64)
        self.assertEqual(
            provenance["input_config"],
            {
                "path": str(config_path.resolve()),
                "sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            },
        )

    def test_dry_run_needs_no_job_or_credentials_and_redacts_values(self):
        result, stdout, stderr = self._run(
            [
                "--dry-run",
                "--agent",
                "custom.agent:Agent",
                "--ak",
                "api_key=do-not-print",
            ],
            environment={"JUDGE_API_KEY": "also-do-not-print"},
        )

        self.assertEqual(result, 0, stderr)
        self.assertFalse(self.invocation_path.exists())
        self.assertFalse((self.root / "jobs").exists())
        self.assertIn("2 task(s)", stdout)
        self.assertIn("custom.agent:Agent", stdout)
        self.assertNotIn("do-not-print", stdout)
        self.assertNotIn("also-do-not-print", stdout)

    def test_dataset_sources_from_cli_or_config_are_rejected_before_launch(self):
        for arguments in (
            ["-p", "/tmp/other"],
            ["-p/tmp/other"],
            ["-dforeign-dataset"],
            ["-torg/foreign-task"],
            ["--repo=org/other"],
        ):
            with self.subTest(arguments=arguments):
                self.invocation_path.unlink(missing_ok=True)
                result, _stdout, stderr = self._run(arguments)
                self.assertEqual(result, 2)
                self.assertIn("task and dataset selection", stderr)
                self.assertFalse(self.invocation_path.exists())

        config_path = self.root / "conflict.yaml"
        config_path.write_text("datasets:\n  - path: somewhere-else\n")
        result, _stdout, stderr = self._run(["--config", str(config_path)])
        self.assertEqual(result, 2)
        self.assertIn("must not define 'tasks' or 'datasets'", stderr)
        self.assertFalse(self.invocation_path.exists())

    def test_missing_manifest_task_fails_without_scanning_or_launching(self):
        missing = self.generation / "tasks" / "main--writer--task2"
        missing.rmdir()
        # An unrelated historical task must not substitute for a manifest task.
        historical = self.root / ".generated" / "old" / "tasks" / missing.name
        historical.mkdir(parents=True)

        result, _stdout, stderr = self._run([])

        self.assertEqual(result, 2)
        self.assertIn("is unavailable", stderr)
        self.assertFalse(self.invocation_path.exists())

    def test_unknown_native_options_are_forwarded_and_native_status_is_returned(self):
        result, _stdout, _stderr = self._run(
            ["--future-native-option", "value with spaces"],
            environment={"FAKE_HARBOR_EXIT_EARLY": "37"},
        )

        self.assertEqual(result, 37)
        invocation = json.loads(self.invocation_path.read_text())
        self.assertEqual(invocation[-2:], ["--future-native-option", "value with spaces"])

    def test_interrupt_after_job_creation_still_records_provenance(self):
        result, _stdout, stderr = self._run(
            ["--job-name", "interrupted-job"],
            environment={"FAKE_HARBOR_INTERRUPT_PARENT": "1"},
        )

        self.assertEqual(result, 130, stderr)
        self.assertTrue(
            (
                self.root
                / "jobs"
                / "interrupted-job"
                / "jobbench_provenance.json"
            ).is_file()
        )

    def test_unsafe_or_existing_job_name_is_rejected_before_launch(self):
        for name in ("../escape", "nested/job"):
            with self.subTest(name=name):
                result, _stdout, stderr = self._run(["--job-name", name])
                self.assertEqual(result, 2)
                self.assertIn("single directory name", stderr)
                self.assertFalse(self.invocation_path.exists())

        existing = self.root / "jobs" / "already-there"
        existing.mkdir(parents=True)
        result, _stdout, stderr = self._run(["--job-name=already-there"])
        self.assertEqual(result, 2)
        self.assertIn("already exists", stderr)
        self.assertFalse(self.invocation_path.exists())

    def test_attached_short_jobs_dir_value_targets_the_actual_job(self):
        jobs_dir = self.root / "attached-results"

        result, _stdout, stderr = self._run(
            [f"-o{jobs_dir}", "--job-name", "attached-job"]
        )

        self.assertEqual(result, 0, stderr)
        self.assertTrue(
            (jobs_dir / "attached-job" / "jobbench_provenance.json").is_file()
        )

    def test_example_config_is_native_and_keeps_per_agent_output_limits(self):
        from harbor.models.job.config import JobConfig

        example_path = HARBOR_ROOT / "configs" / "example.yaml"
        config = JobConfig.model_validate(yaml.safe_load(example_path.read_text()))

        self.assertEqual(config.n_concurrent_trials, 4)
        self.assertEqual(config.agents[0].name, "terminus-2")
        self.assertEqual(
            config.agents[0].kwargs["llm_call_kwargs"]["max_tokens"], 8192
        )

    def test_native_executable_is_the_sibling_of_the_invoked_venv_python(self):
        invoked_python = self.root / ".venv" / "bin" / "python"
        global_python = self.root / "global" / "bin" / "python"
        invoked_python.parent.mkdir(parents=True)
        global_python.parent.mkdir(parents=True)
        global_python.touch()
        invoked_python.symlink_to(global_python)
        (invoked_python.parent / "harbor").touch()

        with mock.patch.object(sys, "executable", str(invoked_python)):
            self.assertEqual(
                evaluate._native_executable(), invoked_python.parent / "harbor"
            )


if __name__ == "__main__":
    unittest.main()
