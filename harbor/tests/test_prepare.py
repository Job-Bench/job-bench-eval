from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


HARBOR_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARBOR_ROOT))

from jobbench_harbor import hf_source, prepare, render


REPO_ID = "example/jobbench"
REVISION = "0123456789abcdef0123456789abcdef01234567"


def remote_source_files(
    split: str = "main", occupation: str = "accountants", number: int = 1
) -> list[str]:
    source_split = "dataset" if split == "main" else "dataset_easy"
    prefix = f"{source_split}/{occupation}/task{number}"
    return [
        f"{prefix}/RUBRICS.json",
        f"{prefix}/task_card.md",
        f"{prefix}/task_folder/TASK_INSTRUCTIONS.txt",
        f"{prefix}/task_folder/input.csv",
    ]


def add_task(
    snapshot: Path,
    split: str,
    occupation: str,
    number: int,
    *,
    prompt: str = "Make the requested report.",
) -> Path:
    source_split = "dataset" if split == "main" else "dataset_easy"
    task = snapshot / source_split / occupation / f"task{number}"
    folder = task / "task_folder"
    folder.mkdir(parents=True)
    (folder / "TASK_INSTRUCTIONS.txt").write_text(prompt, encoding="utf-8")
    (folder / "input.csv").write_bytes(b"name,value\nA,1\n")
    (task / "RUBRICS.json").write_text(
        json.dumps(
            {
                "rubrics": [
                    {
                        "rubric": "A report exists",
                        "criterion": ["The requested report exists"],
                        "weight": 1,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (task / "task_card.md").write_text("# Analyst", encoding="utf-8")
    return task


def fake_render(
    source_dir: Path,
    destination: Path,
    *,
    task_id: str,
    split: str,
    occupation: str,
    task_number: str,
    repo_id: str,
    revision: str,
    judge_path: Path,
    search_files: list[str],
) -> dict:
    if task_number != source_dir.name:
        raise ValueError(f"wrong renderer task number: {task_number}")
    destination.mkdir()
    prompt = (source_dir / "task_folder" / "TASK_INSTRUCTIONS.txt").read_text(
        encoding="utf-8"
    )
    (destination / "rendered.txt").write_text(prompt, encoding="utf-8")
    (destination / "search-files.json").write_text(
        json.dumps(search_files), encoding="utf-8"
    )
    return {
        "id": task_id,
        "split": split,
        "occupation": occupation,
        "task_number": task_number,
        "source_files": [],
    }


class PrepareFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.snapshot = self.base / "snapshot"
        self.root = self.base / "harbor"
        self.judge = self.base / "judge.py"
        self.judge.write_bytes(b"print('judge')\n")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def generate(self, **kwargs: object) -> dict:
        with mock.patch.object(prepare, "render_task", side_effect=fake_render):
            return prepare.generate(
                self.snapshot,
                self.root,
                repo_id=REPO_ID,
                revision=REVISION,
                judge_path=self.judge,
                **kwargs,
            )

    def active_generation(self) -> Path:
        return (self.root / "current").resolve(strict=True)


class GenerationTests(PrepareFixture):
    def common_inputs(self, directory: Path) -> tuple[Path, Path]:
        helpers = directory / "jobbench_eval"
        helpers.mkdir(parents=True)
        (helpers / "__init__.py").write_text("", encoding="utf-8")
        (helpers / "reader.py").write_text("VALUE = 1\n", encoding="utf-8")
        runtime = directory / "calc-runtime" / "Dockerfile"
        runtime.parent.mkdir()
        runtime.write_text("FROM runtime:one\n", encoding="utf-8")
        return helpers, runtime

    def test_helper_and_calc_runtime_changes_regenerate_real_task_packages(self) -> None:
        add_task(self.snapshot, "main", "accountants", 1)
        helpers, runtime = self.common_inputs(self.base / "common-eval")
        with mock.patch.object(render, "JUDGE_HELPERS", helpers), mock.patch.object(
            render, "CALC_RUNTIME_DOCKERFILE", runtime
        ):
            def generate():
                return prepare.generate(
                    self.snapshot, self.root, repo_id=REPO_ID,
                    revision=REVISION, judge_path=self.judge,
                )

            first = generate()
            original = self.active_generation()
            (helpers / "reader.py").write_text("VALUE = 2\n", encoding="utf-8")
            second = generate()

            self.assertNotEqual(first["fingerprint"], second["fingerprint"])
            self.assertNotEqual(first["rendering_sha256"], second["rendering_sha256"])
            self.assertEqual(first["judge_sha256"], second["judge_sha256"])
            relative = "tasks/main--accountants--task1/tests/jobbench_eval/reader.py"
            self.assertEqual((original / relative).read_text(), "VALUE = 1\n")
            self.assertEqual((self.active_generation() / relative).read_text(), "VALUE = 2\n")

            runtime.write_text("FROM runtime:two\n", encoding="utf-8")
            third = generate()
            self.assertNotEqual(second["fingerprint"], third["fingerprint"])
            copied_runtime = self.active_generation() / "tasks/main--accountants--task1/tests/calc-runtime/Dockerfile"
            self.assertEqual(copied_runtime.read_text(), "FROM runtime:two\n")

    def test_common_input_hash_is_location_independent_and_excludes_bytecode(self) -> None:
        first_helpers, first_runtime = self.common_inputs(self.base / "first")
        second_helpers, second_runtime = self.common_inputs(self.base / "second")
        cache = second_helpers / "__pycache__"
        cache.mkdir()
        (cache / "reader.cpython-312.pyc").write_bytes(b"irrelevant cache")
        (second_helpers / "legacy.pyc").write_bytes(b"irrelevant bytecode")

        hashes = []
        for helpers, runtime in ((first_helpers, first_runtime), (second_helpers, second_runtime)):
            with mock.patch.object(render, "JUDGE_HELPERS", helpers), mock.patch.object(
                render, "CALC_RUNTIME_DOCKERFILE", runtime
            ):
                hashes.append(prepare._rendering_hash(self.root))

        self.assertEqual(hashes[0], hashes[1])

    def test_changed_prompt_replaces_active_generation_and_keeps_old_one(self) -> None:
        task = add_task(self.snapshot, "main", "accountants", 1, prompt="old")
        first = self.generate()
        old_generation = self.active_generation()

        (task / "task_folder" / "TASK_INSTRUCTIONS.txt").write_text(
            "new", encoding="utf-8"
        )
        second = self.generate()

        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(old_generation, self.active_generation())
        self.assertEqual(
            (self.root / "tasks" / "main--accountants--task1" / "rendered.txt").read_text(),
            "new",
        )
        self.assertTrue(old_generation.is_dir())
        self.assertEqual(
            (old_generation / "tasks" / "main--accountants--task1" / "rendered.txt").read_text(),
            "old",
        )

    def test_deleted_task_disappears_from_new_generation(self) -> None:
        add_task(self.snapshot, "main", "accountants", 1)
        deleted = add_task(self.snapshot, "main", "accountants", 2)
        self.generate()

        for path in sorted(deleted.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        deleted.rmdir()
        manifest = self.generate()

        self.assertEqual(
            [record["id"] for record in manifest["tasks"]],
            ["main--accountants--task1"],
        )
        self.assertFalse(
            (self.root / "tasks" / "main--accountants--task2").exists()
        )

    def test_main_and_easy_tasks_in_same_slot_have_distinct_ids(self) -> None:
        add_task(self.snapshot, "main", "accountants", 1, prompt="main")
        add_task(self.snapshot, "easy", "accountants", 1, prompt="easy")

        manifest = self.generate()

        self.assertEqual(
            [record["id"] for record in manifest["tasks"]],
            [
                "easy--accountants--task1",
                "main--accountants--task1",
            ],
        )
        self.assertEqual(len(list((self.root / "tasks").iterdir())), 2)

    def test_render_failure_keeps_previous_generation_active(self) -> None:
        task = add_task(self.snapshot, "main", "accountants", 1, prompt="old")
        self.generate()
        active = os.readlink(self.root / "current")
        manifest_before = (self.root / "manifest.json").read_bytes()
        (task / "task_folder" / "TASK_INSTRUCTIONS.txt").write_text(
            "new", encoding="utf-8"
        )

        with mock.patch.object(prepare, "render_task", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                prepare.generate(
                    self.snapshot,
                    self.root,
                    repo_id=REPO_ID,
                    revision=REVISION,
                    judge_path=self.judge,
                )

        self.assertEqual(os.readlink(self.root / "current"), active)
        self.assertEqual((self.root / "manifest.json").read_bytes(), manifest_before)

    def test_unchanged_source_reuses_verified_generation(self) -> None:
        add_task(self.snapshot, "main", "accountants", 1)
        first = self.generate()
        generation = self.active_generation()
        manifest_stat = (generation / "manifest.json").stat()

        second = self.generate()

        self.assertEqual(second, first)
        self.assertEqual(self.active_generation(), generation)
        self.assertEqual((generation / "manifest.json").stat().st_ino, manifest_stat.st_ino)
        self.assertEqual((generation / "manifest.json").stat().st_mtime_ns, manifest_stat.st_mtime_ns)

    def test_corrupt_existing_generation_is_not_reused_or_published(self) -> None:
        add_task(self.snapshot, "main", "accountants", 1)
        self.generate()
        generation = self.active_generation()
        rendered = generation / "tasks" / "main--accountants--task1" / "rendered.txt"
        rendered.write_text("tampered", encoding="utf-8")

        with self.assertRaisesRegex(prepare.GenerationError, "verification"):
            self.generate()

        self.assertEqual(self.active_generation(), generation)

    def test_tampered_manifest_provenance_is_not_reused(self) -> None:
        add_task(self.snapshot, "main", "accountants", 1)
        original = self.generate()
        generation = self.active_generation()
        manifest_path = generation / "manifest.json"

        for mutation in (
            {"revision": "wrong"},
            {"tasks": [], "task_count": 0},
            {"local_note": "untrusted manifest addition"},
        ):
            with self.subTest(mutation=mutation):
                tampered = {**original, **mutation}
                manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
                with self.assertRaisesRegex(prepare.GenerationError, "verification"):
                    self.generate()
                self.assertEqual(self.active_generation(), generation)
                manifest_path.write_text(json.dumps(original), encoding="utf-8")

    def test_manifest_records_source_judge_and_generated_hashes(self) -> None:
        add_task(self.snapshot, "main", "accountants", 1)

        manifest = self.generate()

        self.assertEqual(manifest["repo_id"], REPO_ID)
        self.assertEqual(manifest["revision"], REVISION)
        self.assertEqual(
            manifest["judge_sha256"],
            "d860532b6f44e797d2351bab78e3e28408c6091199437c929ee691a74af3f16a",
        )
        source_paths = {
            item["path"] for item in manifest["tasks"][0]["source_files"]
        }
        self.assertEqual(
            source_paths,
            {
                "RUBRICS.json",
                "task_card.md",
                "task_folder/TASK_INSTRUCTIONS.txt",
                "task_folder/input.csv",
            },
        )
        generated_paths = {item["path"] for item in manifest["generated_files"]}
        self.assertEqual(
            generated_paths,
            {
                "tasks/main--accountants--task1/rendered.txt",
                "tasks/main--accountants--task1/search-files.json",
            },
        )

    def test_remote_listing_marks_search_files_without_local_bytes(self) -> None:
        add_task(self.snapshot, "main", "accountants", 1)
        remote_files = [
            *remote_source_files(),
            "dataset/accountants/task1/files_required_to_search/private-answer.txt",
            "dataset/accountants/task1/model_output/old.txt",
        ]

        self.generate(remote_files=remote_files)

        search_files = json.loads(
            (
                self.root
                / "tasks"
                / "main--accountants--task1"
                / "search-files.json"
            ).read_text()
        )
        self.assertEqual(
            search_files, ["files_required_to_search/private-answer.txt"]
        )

    def test_remote_listing_rejects_stale_extra_local_raw_file(self) -> None:
        task = add_task(self.snapshot, "main", "accountants", 1)
        stale = task / "task_folder" / "stale.csv"
        stale.write_text("old", encoding="utf-8")

        with self.assertRaisesRegex(prepare.SourceError, "extra local"):
            self.generate(remote_files=remote_source_files())

        self.assertFalse((self.root / "current").exists())

    def test_remote_listing_rejects_missing_local_raw_file(self) -> None:
        add_task(self.snapshot, "main", "accountants", 1)
        remote = [
            *remote_source_files(),
            "dataset/accountants/task1/task_folder/new.csv",
        ]

        with self.assertRaisesRegex(prepare.SourceError, "missing local"):
            self.generate(remote_files=remote)

        self.assertFalse((self.root / "current").exists())

    def test_bytecode_does_not_change_rendering_fingerprint(self) -> None:
        runtime = self.root / "runtime"
        runtime.mkdir(parents=True)
        (runtime / "verify.py").write_text("pass\n", encoding="utf-8")
        before = prepare._rendering_hash(self.root)
        cache = runtime / "__pycache__"
        cache.mkdir()
        (cache / "verify.cpython-313.pyc").write_bytes(b"changing bytecode")

        after = prepare._rendering_hash(self.root)

        self.assertEqual(after, before)


class ValidationTests(PrepareFixture):
    def assert_rejected_without_escape(self, expected: str) -> None:
        outside = self.base / "outside.txt"
        with self.assertRaisesRegex((prepare.SourceError, prepare.GenerationError), expected):
            self.generate()
        self.assertFalse(outside.exists())
        self.assertFalse((self.root / "current").exists())

    def test_missing_required_source_file_is_rejected(self) -> None:
        task = add_task(self.snapshot, "main", "accountants", 1)
        (task / "RUBRICS.json").unlink()
        self.assert_rejected_without_escape("RUBRICS")

    def test_malformed_rubric_is_rejected(self) -> None:
        task = add_task(self.snapshot, "main", "accountants", 1)
        (task / "RUBRICS.json").write_text("not json", encoding="utf-8")
        self.assert_rejected_without_escape("RUBRICS")

    def test_structurally_invalid_rubric_is_rejected(self) -> None:
        invalid_documents = (
            {"rubrics": [None]},
            {"rubrics": [{}]},
            {
                "rubrics": [
                    {"rubric": "criterion", "criterion": ["check"], "weight": -1}
                ]
            },
            {
                "rubrics": [
                    {
                        "rubric": "criterion",
                        "criterion": ["check"],
                        "weight": float("nan"),
                    }
                ]
            },
            {
                "rubrics": [
                    {"rubric": "criterion", "criterion": ["check"], "weight": 0}
                ]
            },
        )
        for document in invalid_documents:
            with self.subTest(document=document):
                with tempfile.TemporaryDirectory() as temp:
                    snapshot = Path(temp) / "snapshot"
                    task = add_task(snapshot, "main", "accountants", 1)
                    (task / "RUBRICS.json").write_text(
                        json.dumps(document), encoding="utf-8"
                    )
                    with self.assertRaisesRegex(prepare.SourceError, "RUBRICS"):
                        prepare.generate(
                            snapshot,
                            Path(temp) / "harbor",
                            repo_id=REPO_ID,
                            revision=REVISION,
                            judge_path=self.judge,
                        )

    def test_symlink_in_source_is_rejected(self) -> None:
        task = add_task(self.snapshot, "main", "accountants", 1)
        (self.base / "outside.txt").write_text("secret", encoding="utf-8")
        (task / "task_folder" / "escape.txt").symlink_to(self.base / "outside.txt")

        with self.assertRaisesRegex(prepare.SourceError, "symlink"):
            self.generate()
        self.assertFalse((self.root / "current").exists())

    def test_traversal_in_remote_listing_is_rejected(self) -> None:
        add_task(self.snapshot, "main", "accountants", 1)

        with self.assertRaisesRegex(prepare.SourceError, "unsafe remote path"):
            self.generate(remote_files=["dataset/accountants/task1/../../../../outside.txt"])
        self.assertFalse((self.root / "current").exists())

    def test_unmanaged_stable_path_is_not_replaced(self) -> None:
        add_task(self.snapshot, "main", "accountants", 1)
        self.root.mkdir()
        (self.root / "tasks").mkdir()
        marker = self.root / "tasks" / "keep.txt"
        marker.write_text("mine", encoding="utf-8")

        with self.assertRaisesRegex(prepare.GenerationError, "tasks"):
            self.generate()

        self.assertEqual(marker.read_text(), "mine")
        self.assertFalse((self.root / "current").exists())

    def test_unexpected_current_symlink_is_rejected(self) -> None:
        add_task(self.snapshot, "main", "accountants", 1)
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        self.root.mkdir()
        (self.root / "current").symlink_to(elsewhere)

        with self.assertRaisesRegex(prepare.GenerationError, "current"):
            self.generate()

        self.assertEqual((self.root / "current").resolve(), elsewhere)


class HuggingFaceSourceTests(unittest.TestCase):
    def test_download_resolves_sha_and_allowlists_only_raw_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "hf-cache"
            calls: list[tuple[str, dict]] = []

            raw_files = {
                "dataset/accountants/task1/RUBRICS.json": b'{"rubrics": [{}]}',
                "dataset/accountants/task1/task_card.md": b"# Card\n",
                "dataset/accountants/task1/task_folder/input.csv": b"a,b\n1,2\n",
            }

            class Sibling:
                def __init__(self, name: str, data: bytes | None = None, *, lfs: bool = False) -> None:
                    self.rfilename = name
                    self.size = len(data) if data is not None else 0
                    self.blob_id = (
                        "lfs-pointer-hash-is-not-the-content-hash"
                        if data is not None and lfs
                        else hashlib.sha1(
                            f"blob {len(data)}\0".encode("ascii") + data
                        ).hexdigest()
                        if data is not None
                        else None
                    )
                    self.lfs = (
                        SimpleNamespace(sha256=hashlib.sha256(data).hexdigest())
                        if data is not None and lfs
                        else None
                    )

            class Info:
                sha = REVISION
                siblings = [
                    Sibling(name, data, lfs=name.endswith("input.csv"))
                    for name, data in raw_files.items()
                ] + [
                    Sibling("dataset/accountants/task1/files_required_to_search/answer.txt"),
                    Sibling("dataset/accountants/task1/model_output/old.txt"),
                ]

            class Api:
                def dataset_info(self, repo_id: str, **kwargs: object) -> Info:
                    calls.append(("info", {"repo_id": repo_id, **kwargs}))
                    return Info()

            def downloader(**kwargs: object) -> str:
                calls.append(("download", kwargs))
                snapshot = Path(temp) / "snapshot"
                for name, data in raw_files.items():
                    target = snapshot / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                return str(snapshot)

            result = hf_source.fetch_snapshot(
                destination,
                repo_id=REPO_ID,
                revision="main",
                api=Api(),
                snapshot_downloader=downloader,
                retries=1,
            )

            self.assertEqual(result.revision, REVISION)
            self.assertEqual(result.path, Path(temp) / "snapshot")
            self.assertEqual(
                result.remote_files,
                [
                    "dataset/accountants/task1/RUBRICS.json",
                    "dataset/accountants/task1/files_required_to_search/answer.txt",
                    "dataset/accountants/task1/model_output/old.txt",
                    "dataset/accountants/task1/task_card.md",
                    "dataset/accountants/task1/task_folder/input.csv",
                ],
            )
            self.assertEqual(
                calls,
                [
                    (
                        "info",
                        {
                            "repo_id": REPO_ID,
                            "revision": "main",
                            "files_metadata": True,
                        },
                    ),
                    (
                        "download",
                        {
                            "repo_id": REPO_ID,
                            "repo_type": "dataset",
                            "revision": REVISION,
                            "local_dir": str(destination / REVISION),
                            "allow_patterns": [
                                "dataset/*/task*/task_folder/**",
                                "dataset/*/task*/RUBRICS.json",
                                "dataset/*/task*/task_card.md",
                                "dataset_easy/*/task*/task_folder/**",
                                "dataset_easy/*/task*/RUBRICS.json",
                                "dataset_easy/*/task*/task_card.md",
                            ],
                        },
                    ),
                ],
            )

    def test_download_rejects_cached_source_bytes_that_do_not_match_hf(self) -> None:
        expected = b"authoritative"

        class Sibling:
            rfilename = "dataset/accountants/task1/task_folder/input.csv"
            size = len(expected)
            blob_id = hashlib.sha1(
                f"blob {len(expected)}\0".encode("ascii") + expected
            ).hexdigest()
            lfs = None

        class Info:
            sha = REVISION
            siblings = [Sibling()]

        class Api:
            def dataset_info(self, repo_id: str, **kwargs: object) -> Info:
                return Info()

        with tempfile.TemporaryDirectory() as temp:
            def downloader(**kwargs: object) -> str:
                snapshot = Path(temp) / REVISION
                target = snapshot / Sibling.rfilename
                target.parent.mkdir(parents=True)
                target.write_bytes(b"edited-source")
                return str(snapshot)

            with self.assertRaisesRegex(RuntimeError, "does not match"):
                hf_source.fetch_snapshot(
                    Path(temp),
                    repo_id=REPO_ID,
                    revision="main",
                    api=Api(),
                    snapshot_downloader=downloader,
                    retries=1,
                )

    def test_transient_resolution_failure_has_bounded_retry(self) -> None:
        attempts = 0

        class Api:
            def dataset_info(self, repo_id: str, **kwargs: object) -> object:
                nonlocal attempts
                attempts += 1
                raise OSError("temporary")

        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(OSError, "temporary"):
                hf_source.fetch_snapshot(
                    Path(temp),
                    repo_id=REPO_ID,
                    revision="main",
                    api=Api(),
                    snapshot_downloader=lambda **kwargs: "unused",
                    retries=2,
                    retry_delay=0,
                )

        self.assertEqual(attempts, 2)


class CliTests(unittest.TestCase):
    def test_default_output_is_concise_and_reports_generated_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "harbor"
            snapshot = Path(temp) / "snapshot"
            snapshot.mkdir()
            source = hf_source.HFSnapshot(snapshot, REVISION, [])
            manifest = {
                "fingerprint": "a" * 64,
                "revision": REVISION,
                "task_count": 3,
                "tasks": [{"split": "main"}, {"split": "main"}, {"split": "easy"}],
                "generated_files": [{"path": f"large/{index}"} for index in range(100)],
            }
            stdout = io.StringIO()

            with (
                mock.patch.object(prepare, "fetch_snapshot", return_value=source),
                mock.patch.object(prepare, "generate", return_value=manifest),
                contextlib.redirect_stdout(stdout),
            ):
                result = prepare.main([], root=root)

            output = stdout.getvalue()
            self.assertEqual(result, 0)
            self.assertIn("Generated 3 tasks (easy=1, main=2)", output)
            self.assertIn(REVISION, output)
            self.assertIn(f".generated/{'a' * 64}", output)
            self.assertNotIn("generated_files", output)
            self.assertLess(len(output), 400)


if __name__ == "__main__":
    unittest.main()
