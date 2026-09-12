"""Exercise ordinary OpenCode attempt logging without launching an agent."""

import csv
from pathlib import Path
import subprocess
import tempfile
import unittest


RUNNER = Path(__file__).resolve().parents[1] / "eval/run_benchmark_opencode.sh"


class OpenCodeAttemptIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.trajectory = self.root / "attempt trajectory.jsonl"

    def run_shell(self, script, *args):
        result = subprocess.run(
            ["bash", "-c", 'source "$1"\n' + script, "test", str(RUNNER), *map(str, args)],
            text=True, capture_output=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_duplicate_event_session_ids_produce_one_complete_attempt_row(self):
        # OpenCode emits the same sessionID on the event and its nested part.
        self.trajectory.write_text(
            '{"type":"step_start","timestamp":1789177854553,"sessionID":"ses_first",'
            '"part":{"id":"prt_start","messageID":"msg_first","sessionID":"ses_first",'
            '"snapshot":"489850e3bdec1655617d70367f9be2509769a4e5","type":"step-start"}}\n'
            '{"type":"text","sessionID":"ses_later","part":{"sessionID":"ses_later"}}\n'
        )
        index = self.root / "attempt_index.tsv"
        self.run_shell(
            '''
session_id="$(extract_session_id_from_traj "$2")"
append_attempt_index "$3" profession_task1 provider/model model 1 success 0 900 \
    "$session_id" 2026-09-12T01:50:46Z 2026-09-12T01:59:51Z "$2"
''',
            self.trajectory, index,
        )
        self.assertEqual(len(index.read_text().splitlines()), 2)
        with index.open(newline="") as stream:
            rows = list(csv.reader(stream, delimiter="\t"))
        self.assertEqual(len(rows[0]), 11)
        self.assertEqual(rows[1], [
            "profession_task1", "provider/model", "model", "1", "success", "0", "900",
            "ses_first", "2026-09-12T01:50:46Z", "2026-09-12T01:59:51Z", str(self.trajectory),
        ])

    def test_session_id_allows_json_whitespace(self):
        self.trajectory.write_text(
            '{"type": "step_start", "sessionID" : "ses_first", '
            '"part": {"sessionID": "ses_first"}}\n'
        )
        self.assertEqual(
            self.run_shell('extract_session_id_from_traj "$2"', self.trajectory),
            "ses_first\n",
        )

    def test_absent_session_id_returns_empty_successfully(self):
        for content in (None, "", '{"type":"error","error":{"message":"failed"}}\n'):
            with self.subTest(content=content):
                if content is not None:
                    self.trajectory.write_text(content)
                self.assertEqual(
                    self.run_shell('extract_session_id_from_traj "$2"', self.trajectory),
                    "",
                )


if __name__ == "__main__":
    unittest.main()
