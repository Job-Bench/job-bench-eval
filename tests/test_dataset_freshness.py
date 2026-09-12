"""Offline CLI coverage, replacing only the external Hugging Face SDK."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


HELPER = Path(__file__).resolve().parents[1] / "scripts/check_dataset_freshness.py"
LOCAL_SHA = "1" * 40
LATEST_SHA = "2" * 40


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class DatasetFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(HELPER.is_file(), "the advisory freshness checker is missing")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.dataset = self.base / "dataset"
        self.fake = self.base / "fake-sdk"
        self.env = dict(os.environ, PYTHONPATH=str(self.fake),
                        PYTHONDONTWRITEBYTECODE="1", FAKE_HF_SHA=LOCAL_SHA,
                        FAKE_HF_REPO="org/bench", FAKE_HF_MODE="ok")
        self.env.pop("JOBBENCH_SKIP_HF_CHECK", None)
        write(self.fake, "huggingface_hub.py", '''
import os
import sys
import time
from types import SimpleNamespace

class HfApi:
    def dataset_info(self, *, repo_id, revision, files_metadata, timeout):
        assert repo_id == os.environ["FAKE_HF_REPO"]
        assert revision == "main"
        assert files_metadata is False
        assert 0 < timeout <= 5
        mode = os.environ["FAKE_HF_MODE"]
        if mode == "hang":
            time.sleep(30)
        if mode == "error":
            print("SECRET_TOKEN=hf_do_not_print", file=sys.stderr)
            raise RuntimeError("SECRET_TOKEN=hf_do_not_print")
        return SimpleNamespace(sha=os.environ["FAKE_HF_SHA"])
''')

    def managed(self, *, repo_id="org/bench", revision=LOCAL_SHA):
        state = {"version": 1, "repo_id": repo_id, "revision": revision, "files": {}}
        for split in ("main", "easy"):
            relative = f"{split}/plumbers/task1/task_card.md"
            write(self.dataset, relative, "local task content")
            state["files"][relative] = "a" * 64
        return write(self.dataset, ".setup/state.json", json.dumps(state))

    def manifest(self, name="generation", *, repo_id="org/bench", revision=LOCAL_SHA):
        return write(self.base, f"{name}/manifest.json", json.dumps({
            "repo_id": repo_id, "revision": revision, "tasks": [],
            "generated_files": [], "judge_sha256": "a" * 64,
        }))

    def check(self, *args, env=None):
        result = subprocess.run([sys.executable, str(HELPER), *map(str, args)],
                                cwd=self.base, env=self.env if env is None else env,
                                capture_output=True, text=True, timeout=9)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        return result.stdout + result.stderr

    def test_current_managed_split_and_nested_target_are_confirmed(self):
        self.managed()
        for relative in ("main", "easy", "main/plumbers", "easy/plumbers/task1"):
            with self.subTest(relative=relative):
                output = self.check("--dataset-root", self.dataset / relative)
                self.assertIn("matches Hugging Face main", output)
                self.assertIn(LOCAL_SHA, output)
                self.assertNotIn("WARNING", output)

    def test_stale_root_warns_with_both_revisions_and_root_setup(self):
        self.managed()
        self.env["FAKE_HF_SHA"] = LATEST_SHA
        output = self.check("--dataset-root", self.dataset / "main")
        self.assertIn("WARNING", output)
        self.assertIn("differs", output)
        self.assertIn(LOCAL_SHA, output)
        self.assertIn(LATEST_SHA, output)
        self.assertIn("./setup.sh", output)
        self.assertNotIn("./harbor/setup.sh", output)

    def test_fixed_old_harbor_generation_is_checked_instead_of_current_pointer(self):
        old = self.manifest("generations/old")
        current = self.manifest("generations/new", revision=LATEST_SHA)
        (self.base / "current").symlink_to(current.parent, target_is_directory=True)
        self.env["FAKE_HF_SHA"] = LATEST_SHA
        output = self.check("--manifest", old)
        self.assertIn("WARNING", output)
        self.assertIn(LOCAL_SHA, output)
        self.assertIn(LATEST_SHA, output)
        self.assertIn("./harbor/setup.sh", output)

    def test_manifest_repository_overrides_environment_default(self):
        manifest = self.manifest(repo_id="other/source")
        self.env.update(FAKE_HF_REPO="other/source", JOBBENCH_HF_REPO_ID="wrong/repo")
        output = self.check("--manifest", manifest)
        self.assertIn("matches Hugging Face main", output)
        self.assertIn("other/source", output)
        self.assertNotIn("wrong/repo", output)

    def test_missing_local_provenance_warns_without_network_delay(self):
        self.env["FAKE_HF_MODE"] = "hang"
        start = time.monotonic()
        output = self.check("--dataset-root", self.dataset / "main")
        self.assertLess(time.monotonic() - start, 2)
        self.assertIn("cannot verify", output)
        self.assertIn("./setup.sh", output)

    def test_custom_subset_does_not_borrow_default_or_ancestor_manifest(self):
        self.managed()
        self.env["FAKE_HF_MODE"] = "hang"
        for relative in ("subset", "main/custom_subset", "main/plumbers/task99"):
            with self.subTest(relative=relative):
                target = self.dataset / relative
                target.mkdir(parents=True)
                start = time.monotonic()
                output = self.check("--dataset-root", target)
                self.assertLess(time.monotonic() - start, 2)
                self.assertIn("cannot verify", output)
                self.assertNotIn("matches", output)

    def test_malformed_provenance_and_non_sha_revision_are_advisory(self):
        state = self.managed()
        valid = json.loads(state.read_text())
        for value in ("not JSON", "[]", json.dumps(dict(valid, revision="main")),
                      json.dumps(dict(valid, repo_id="bad\nSECRET_TOKEN")),
                      json.dumps(dict(valid, version=2)),
                      json.dumps(dict(valid, files=[])),
                      json.dumps(dict(valid, files={"main/local.txt": "a" * 64})),
                      json.dumps(dict(valid, files={
                          "main/plumbers/task1/task_card.md": "invalid-hash"})),
                      json.dumps(dict(valid, files={
                          "main/plumbers/task1/task_folder/../task_card.md": "a" * 64}))):
            with self.subTest(value=value):
                state.write_text(value)
                output = self.check("--dataset-root", self.dataset / "main")
                self.assertIn("cannot verify", output)
                self.assertNotIn("SECRET_TOKEN", output)

    def test_missing_or_invalid_harbor_manifest_gives_harbor_guidance(self):
        manifest = self.base / "missing/manifest.json"
        output = self.check("--manifest", manifest)
        self.assertIn("cannot verify", output)
        self.assertIn("./harbor/setup.sh", output)
        manifest = self.manifest(revision="main")
        output = self.check("--manifest", manifest)
        self.assertIn("cannot verify", output)
        self.assertIn("./harbor/setup.sh", output)

    def test_network_failure_remains_advisory_and_does_not_leak_sdk_output(self):
        manifest = self.manifest()
        self.env["FAKE_HF_MODE"] = "error"
        output = self.check("--manifest", manifest)
        self.assertIn("cannot verify", output)
        self.assertNotIn("SECRET_TOKEN", output)
        self.assertNotIn("hf_do_not_print", output)

    def test_invalid_remote_revision_is_not_treated_as_stale(self):
        manifest = self.manifest()
        self.env["FAKE_HF_SHA"] = "SECRET_TOKEN"
        output = self.check("--manifest", manifest)
        self.assertIn("cannot verify", output)
        self.assertNotIn("SECRET_TOKEN", output)
        self.assertNotIn("differs", output)

    def test_hung_sdk_is_killed_within_bounded_wall_clock(self):
        manifest = self.manifest()
        self.env["FAKE_HF_MODE"] = "hang"
        start = time.monotonic()
        output = self.check("--manifest", manifest)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 7, output)
        self.assertIn("cannot verify", output)
        self.assertIn("timed out", output)

    def test_skip_is_silent_even_with_missing_provenance_and_hung_sdk(self):
        self.env.update(JOBBENCH_SKIP_HF_CHECK="1", FAKE_HF_MODE="hang")
        start = time.monotonic()
        output = self.check("--dataset-root", self.base / "missing")
        self.assertEqual(output, "")
        self.assertLess(time.monotonic() - start, 2)

    def test_check_preserves_all_local_files_and_does_not_read_task_payloads(self):
        self.managed()
        payload = self.dataset / "main/plumbers/task1/task_card.md"
        payload.unlink()
        os.mkfifo(payload)  # Reading task content would hang; only metadata is relevant.
        write(self.dataset, "main/plumbers/task1/model_output/run/result", "keep results")
        write(self.dataset, "main/plumbers/task1/model_traj/trace", "keep trace")
        before = {str(p): (p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_mode)
                  for p in self.dataset.rglob("*")}
        output = self.check("--dataset-root", self.dataset / "main")
        after = {str(p): (p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_mode)
                 for p in self.dataset.rglob("*")}
        self.assertEqual(before, after)
        self.assertIn("matches Hugging Face main", output)


if __name__ == "__main__":
    unittest.main()
