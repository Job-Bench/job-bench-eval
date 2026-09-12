"""Offline integration tests; the fake replaces only the Hugging Face boundary."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


HELPER = Path(__file__).resolve().parents[1] / "scripts" / "sync_dataset.py"
sync = None
if HELPER.exists():
    spec = importlib.util.spec_from_file_location("sync_dataset", HELPER)
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)

SHA1 = "1" * 40
SHA2 = "2" * 40


def write(root, relative, data):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


def task(root, split="dataset", name="plumbers/task1", text="original"):
    base = f"{split}/{name}"
    write(root, f"{base}/task_card.md", f"# {text}")
    write(root, f"{base}/task_folder/TASK_INSTRUCTIONS.txt", text)
    write(root, f"{base}/task_folder/input.csv", "a,b\n1,2\n")
    write(root, f"{base}/RUBRICS.json", json.dumps({"rubrics": [
        {"rubric": "Correct answer", "weight": 1, "criterion": ["Check result"]}
    ]}))


def tree(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for split in ("main", "easy") for p in (root / split).rglob("*")
            if p.is_file()}


class FakeHF:
    def __init__(self, source, sha=SHA1):
        self.source = source
        self.sha = sha
        self.resolutions = []
        self.downloads = []
        self.omit = None
        self.fail = False
        self.extra_metadata = []

    def dataset_info(self, *, repo_id, revision, files_metadata):
        self.resolutions.append((repo_id, revision, files_metadata))
        siblings = []
        for path in self.source.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                siblings.append(SimpleNamespace(
                    rfilename=path.relative_to(self.source).as_posix(), size=len(data),
                    blob_id=hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest(),
                    lfs=None))
        return SimpleNamespace(sha=self.sha, siblings=siblings + self.extra_metadata)

    def download(self, *, repo_id, repo_type, revision, local_dir, allow_patterns):
        self.downloads.append((repo_id, repo_type, revision, allow_patterns))
        if self.fail:
            raise OSError("simulated download failure")
        # Never silently accept a floating revision at the download boundary.
        if revision != self.sha:
            raise AssertionError("download was not pinned to the resolved SHA")
        destination = Path(local_dir)
        shutil.copytree(self.source, destination, dirs_exist_ok=True, symlinks=True)
        if self.omit:
            (destination / self.omit).unlink()
        return str(destination)


class DatasetRefreshTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(sync, "the source refresh helper has not been implemented")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "remote"
        self.dest = self.base / "active"
        task(self.source)
        task(self.source, "dataset_easy")
        self.hf = FakeHF(self.source)

    def refresh(self, **kwargs):
        return sync.refresh(self.dest, repo_id="org/bench", revision="main",
                            api=self.hf, snapshot_downloader=self.hf.download, **kwargs)

    def state(self):
        return json.loads((self.dest / ".setup/state.json").read_text())

    def backups(self, relative):
        return list((self.dest / ".setup/backups").glob(f"*/{relative}"))

    def test_initial_install_pins_both_splits_and_includes_search_sources(self):
        write(self.source, "dataset/plumbers/task1/files_required_to_search/reference.pdf", b"pdf")
        result = self.refresh()
        self.assertEqual(result["revision"], SHA1)
        self.assertEqual(result["tasks"], {"main": 1, "easy": 1})
        self.assertEqual(result["added"], 9)
        self.assertEqual((self.dest / "main/plumbers/task1/task_card.md").read_text(), "# original")
        self.assertEqual((self.dest / "main/plumbers/task1/files_required_to_search/reference.pdf").read_bytes(), b"pdf")
        self.assertEqual(self.hf.resolutions, [("org/bench", "main", True)])
        self.assertEqual(len(self.hf.downloads), 1)
        self.assertEqual(self.hf.downloads[0][2:], (SHA1, ["dataset/**", "dataset_easy/**"]))
        self.assertEqual(self.state()["revision"], SHA1)

    def test_refresh_updates_adds_deletes_sources_and_preserves_local_history(self):
        self.refresh()
        local = "main/plumbers/task1/"
        for relative in ("model_output/run/result.txt", "model_traj/run/trace.json",
                         "eval_result/judge.json", "notes.txt", "task_folder/local-only.txt"):
            write(self.dest, local + relative, "keep " + relative)
        write(self.dest, "main/summary.json", "keep summary")
        before = tree(self.dest)
        write(self.dest, local + "task_card.md", "my local edit")
        write(self.source, "dataset/plumbers/task1/task_card.md", "# updated")
        (self.source / "dataset/plumbers/task1/task_folder/input.csv").unlink()
        write(self.source, "dataset/plumbers/task1/task_folder/new.csv", "new")
        task(self.source, name="plumbers/task2", text="new task")
        self.hf.sha = SHA2
        result = self.refresh()
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["added"], 5)
        self.assertFalse((self.dest / local / "task_folder/input.csv").exists())
        self.assertEqual((self.dest / local / "task_card.md").read_text(), "# updated")
        self.assertEqual(self.backups("sources/" + local + "task_card.md")[0].read_text(), "my local edit")
        self.assertEqual(self.backups("sources/" + local + "task_folder/input.csv")[0].read_bytes(), b"a,b\n1,2\n")
        for relative, data in before.items():
            if relative.endswith(("task_card.md", "task_folder/input.csv")):
                continue
            self.assertEqual((self.dest / relative).read_bytes(), data)
        self.assertEqual(result["tasks"], {"main": 2, "easy": 1})

    def test_removed_task_is_archived_intact_outside_active_splits(self):
        task(self.source, name="plumbers/task2")
        self.refresh()
        write(self.dest, "main/plumbers/task2/model_output/run/result", "important")
        write(self.dest, "main/plumbers/task2/task_card.md", "edited retired task")
        write(self.dest, "main/plumbers/task2/notes", "local")
        shutil.rmtree(self.source / "dataset/plumbers/task2")
        self.hf.sha = SHA2
        result = self.refresh()
        self.assertEqual(result["retired_tasks"], 1)
        self.assertFalse((self.dest / "main/plumbers/task2").exists())
        archive = self.backups("retired/main/plumbers/task2")[0]
        self.assertEqual((archive / "model_output/run/result").read_text(), "important")
        self.assertEqual((archive / "task_card.md").read_text(), "edited retired task")
        self.assertEqual((archive / "notes").read_text(), "local")

    def test_legacy_adoption_manages_only_recognized_source_paths(self):
        shutil.copytree(self.source / "dataset", self.dest / "main")
        shutil.copytree(self.source / "dataset_easy", self.dest / "easy")
        write(self.dest, "main/plumbers/task1/task_folder/obsolete.csv", "old source")
        write(self.dest, "main/plumbers/task1/model_output/run/result", "keep")
        write(self.dest, "main/plumbers/task1/custom.json", "keep")
        write(self.dest, "main/plumbers/task77/local.txt", "unrecognized local task")
        write(self.dest, "easy/summary.json", "keep")
        write(self.dest, "main/plumbers/task1/task_card.md", "legacy edit")
        result = self.refresh()
        self.assertEqual(self.backups("sources/main/plumbers/task1/task_card.md")[0].read_text(), "legacy edit")
        self.assertEqual(self.backups("sources/main/plumbers/task1/task_folder/obsolete.csv")[0].read_text(), "old source")
        self.assertEqual((self.dest / "main/plumbers/task1/custom.json").read_text(), "keep")
        self.assertEqual((self.dest / "main/plumbers/task77/local.txt").read_text(), "unrecognized local task")
        self.assertEqual((self.dest / "easy/summary.json").read_text(), "keep")
        self.assertEqual(result["retired_tasks"], 0)

    def test_legacy_task_with_only_source_directory_is_retired(self):
        write(self.dest, "main/plumbers/task7/task_folder/TASK_INSTRUCTIONS.txt", "old task")
        write(self.dest, "main/plumbers/task7/model_output/run/result", "keep history")
        result = self.refresh()
        self.assertEqual(result["retired_tasks"], 1)
        self.assertFalse((self.dest / "main/plumbers/task7").exists())
        archive = self.backups("retired/main/plumbers/task7")[0]
        self.assertEqual((archive / "task_folder/TASK_INSTRUCTIONS.txt").read_text(), "old task")
        self.assertEqual((archive / "model_output/run/result").read_text(), "keep history")

    def test_unchanged_rerun_leaves_files_and_backups_untouched_but_checks_hf(self):
        self.refresh()
        path = self.dest / "main/plumbers/task1/task_card.md"
        before = path.stat().st_mtime_ns
        result = self.refresh()
        self.assertEqual([result[key] for key in ("added", "updated", "deleted", "retired_tasks")], [0, 0, 0, 0])
        self.assertIsNone(result["backup"])
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(len(self.hf.resolutions), 2)

    def test_download_and_schema_failures_leave_active_files_and_state_unchanged(self):
        self.refresh()
        original = tree(self.dest)
        original_state = self.state()
        self.hf.sha = SHA2
        for failure in ("download", "missing", "schema", "hash"):
            with self.subTest(failure=failure):
                task(self.source)
                self.hf.fail = failure == "download"
                self.hf.omit = "dataset/plumbers/task1/task_folder/input.csv" if failure == "missing" else None
                if failure == "schema":
                    write(self.source, "dataset/plumbers/task1/RUBRICS.json", '{"rubrics": []}')
                if failure == "hash":
                    original_download = self.hf.download
                    def corrupt(**kwargs):
                        path = original_download(**kwargs)
                        write(Path(path), "dataset/plumbers/task1/task_card.md", "# tampered")
                        return path
                    downloader = corrupt
                else:
                    downloader = self.hf.download
                with self.assertRaises((OSError, ValueError, RuntimeError)):
                    sync.refresh(self.dest, repo_id="org/bench", revision="main",
                                 api=self.hf, snapshot_downloader=downloader)
                self.assertEqual(tree(self.dest), original)
                self.assertEqual(self.state(), original_state)

    def test_rejects_remote_traversal_before_downloading(self):
        self.hf.extra_metadata = [SimpleNamespace(rfilename="dataset/../../escape")]
        with self.assertRaisesRegex((ValueError, RuntimeError), "[Pp]ath|unsafe"):
            self.refresh()
        self.assertEqual(self.hf.downloads, [])
        self.assertEqual(tree(self.dest), {})

    def test_refuses_active_symlink_and_preserves_target(self):
        self.refresh()
        victim = write(self.base, "outside.txt", "do not touch")
        target = self.dest / "main/plumbers/task1/task_card.md"
        target.unlink()
        target.symlink_to(victim)
        self.hf.sha = SHA2
        with self.assertRaisesRegex((ValueError, RuntimeError), "symlink"):
            self.refresh()
        self.assertEqual(victim.read_text(), "do not touch")
        self.assertTrue(target.is_symlink())
        self.assertEqual(self.state()["revision"], SHA1)

    def test_refuses_symlink_in_downloaded_source(self):
        victim = write(self.base, "outside.txt", "do not touch")
        path = self.source / "dataset/plumbers/task1/task_card.md"
        path.unlink()
        path.symlink_to(victim)
        with self.assertRaisesRegex((ValueError, RuntimeError), "symlink"):
            self.refresh()
        self.assertEqual(tree(self.dest), {})

    def test_lfs_sources_are_checked_against_sha256(self):
        metadata = self.hf.dataset_info
        def lfs_info(**kwargs):
            info = metadata(**kwargs)
            for sibling in info.siblings:
                if sibling.rfilename.endswith("input.csv"):
                    sibling.blob_id = "a" * 40  # LFS pointer hash, not file bytes.
                    sibling.lfs = SimpleNamespace(sha256=hashlib.sha256(b"a,b\n1,2\n").hexdigest())
            return info
        self.hf.dataset_info = lfs_info
        self.refresh()
        original = tree(self.dest)
        original_download = self.hf.download
        def corrupt_lfs(**kwargs):
            path = original_download(**kwargs)
            write(Path(path), "dataset/plumbers/task1/task_folder/input.csv", "a,b\n3,4\n")
            return path
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            sync.refresh(self.dest, repo_id="org/bench", api=self.hf,
                         snapshot_downloader=corrupt_lfs)
        self.assertEqual(tree(self.dest), original)

    def test_manifest_rejects_malformed_and_non_source_paths(self):
        self.refresh()
        before = tree(self.dest)
        for manifest in ([], {"version": 1, "files": {"../outside": "0" * 64}},
                         {"version": 1, "files": {"main/plumbers/task1/model_output/result": "0" * 64}}):
            with self.subTest(manifest=manifest):
                write(self.dest, ".setup/state.json", json.dumps(manifest))
                with self.assertRaises(ValueError):
                    self.refresh()
                self.assertEqual(tree(self.dest), before)

    def test_missing_required_instructions_and_empty_split_are_rejected(self):
        self.refresh()
        before = tree(self.dest)
        self.hf.sha = SHA2
        (self.source / "dataset/plumbers/task1/task_folder/TASK_INSTRUCTIONS.txt").unlink()
        with self.assertRaisesRegex(ValueError, "required source"):
            self.refresh()
        self.assertEqual(tree(self.dest), before)
        task(self.source)
        shutil.rmtree(self.source / "dataset_easy/plumbers")
        self.hf.sha = "3" * 40
        with self.assertRaisesRegex(ValueError, "no tasks"):
            self.refresh()
        self.assertEqual(tree(self.dest), before)

    def test_new_source_collision_and_local_edit_are_backed_up(self):
        self.refresh()
        write(self.dest, "main/plumbers/task1/task_folder/new.csv", "my local file")
        write(self.dest, "main/plumbers/task1/task_card.md", "my local card")
        write(self.source, "dataset/plumbers/task1/task_folder/new.csv", "upstream file")
        self.hf.sha = SHA2
        self.refresh()
        self.assertEqual(self.backups("sources/main/plumbers/task1/task_folder/new.csv")[0].read_text(), "my local file")
        self.assertEqual(self.backups("sources/main/plumbers/task1/task_card.md")[0].read_text(), "my local card")
        self.assertEqual((self.dest / "main/plumbers/task1/task_folder/new.csv").read_text(), "upstream file")

    def test_file_directory_conflicts_abort_before_any_active_change(self):
        self.refresh()
        before = tree(self.dest)
        write(self.source, "dataset/plumbers/task1/task_card.md", "changed")
        path = self.source / "dataset/plumbers/task1/task_folder/input.csv"
        path.unlink()
        write(path, "nested.txt", "nested")
        self.hf.sha = SHA2
        with self.assertRaisesRegex(ValueError, "directory"):
            self.refresh()
        self.assertEqual(tree(self.dest), before)

    def test_symlink_in_setup_or_active_parent_is_rejected(self):
        self.dest.mkdir()
        outside = self.base / "outside"
        outside.mkdir()
        (self.dest / ".setup").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.refresh()
        self.assertEqual(list(outside.iterdir()), [])
        (self.dest / ".setup").unlink()
        self.refresh()
        before = tree(self.dest)
        path = self.dest / "main/plumbers/task1/task_folder"
        path.rename(outside / "original")
        path.symlink_to(outside / "original", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.refresh()
        self.assertTrue(path.is_symlink())
        self.assertEqual((outside / "original/input.csv").read_bytes(), b"a,b\n1,2\n")
        self.assertEqual((self.dest / "main/plumbers/task1/task_card.md").read_bytes(),
                         before["main/plumbers/task1/task_card.md"])

    def test_setup_forwards_revision_and_force_preserves_existing_dataset(self):
        checkout = self.base / "checkout"
        checkout.mkdir()
        shutil.copy2(HELPER.parent.parent / "setup.sh", checkout / "setup.sh")
        write(checkout, "dataset/main/summary.json", "existing history")
        trace = self.base / "trace.jsonl"
        stub = (f"#!{sys.executable}\nimport json, os, sys\n"
                "with open(os.environ['TRACE'], 'a') as f:\n"
                " f.write(json.dumps({'args': sys.argv, 'env': os.environ.get('UV_PROJECT_ENVIRONMENT')}) + '\\n')\n")
        for relative in ("bin/uv", ".venv/bin/python"):
            path = write(checkout, relative, stub)
            path.chmod(0o755)
        environment = {**os.environ, "PATH": str(checkout / "bin") + os.pathsep + os.environ["PATH"],
                       "FORCE": "1", "TRACE": str(trace), "UV_PROJECT_ENVIRONMENT": "/wrong/environment"}
        result = subprocess.run(["bash", str(checkout / "setup.sh"), "--revision", "release-tag",
                                 "--repo-id", "org/other"], env=environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((checkout / "dataset/main/summary.json").read_text(), "existing history")
        calls = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["args"][1:], ["sync", "--locked", "--project", str(checkout)])
        self.assertEqual(calls[0]["env"], str(checkout / ".venv"))
        self.assertEqual(calls[1]["args"][1:], [str(checkout / "scripts/sync_dataset.py"),
                                               "--revision", "release-tag", "--repo-id", "org/other"])

    def test_filesystem_failure_rolls_back_source_changes_and_task_retirement(self):
        task(self.source, name="plumbers/task2")
        self.refresh()
        write(self.dest, "main/plumbers/task2/model_output/run/result", "keep")
        before = tree(self.dest)
        state_before = self.state()
        write(self.source, "dataset/plumbers/task1/task_card.md", "changed")
        shutil.rmtree(self.source / "dataset/plumbers/task2")
        self.hf.sha = SHA2
        replace = os.replace
        failed = False
        def fail_state(source, destination):
            nonlocal failed
            if Path(destination) == self.dest / ".setup/state.json" and not failed:
                failed = True
                raise OSError("simulated state write failure")
            return replace(source, destination)
        with patch.object(sync.os, "replace", side_effect=fail_state):
            with self.assertRaisesRegex(OSError, "simulated state write failure"):
                self.refresh()
        self.assertTrue(failed)
        self.assertEqual(tree(self.dest), before)
        self.assertEqual(self.state(), state_before)


if __name__ == "__main__":
    unittest.main()
