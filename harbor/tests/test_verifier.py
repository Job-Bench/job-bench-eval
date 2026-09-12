from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


HARBOR_ROOT = Path(__file__).resolve().parents[1]
VERIFY = HARBOR_ROOT / "runtime" / "verify.py"
ORIGINAL_JUDGE = HARBOR_ROOT.parent / "eval" / "judge.py"


class JudgeHandler(BaseHTTPRequestHandler):
    response_mode = "valid"
    requests: list[dict] = []

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).requests.append(payload)
        if type(self).response_mode == "api-error":
            self.send_response(503)
            body = b'{"error":{"message":"judge unavailable"}}'
        else:
            self.send_response(200)
            if type(self).response_mode == "malformed":
                content = "this is not judge JSON"
            elif type(self).response_mode == "schema-malformed":
                content = "{}"
            else:
                user = payload["messages"][1]["content"]
                text = user if isinstance(user, str) else json.dumps(user)
                passed = "pass-rubric" in text
                content = json.dumps(
                    {
                        "criteria_results": [
                            {
                                "index": 0,
                                "passed": passed,
                                "reasoning": "deterministic local judge",
                                "evidence": "output.txt",
                            }
                        ],
                        "rubric_passed": passed,
                        "overall_reasoning": "local result",
                    }
                )
            body = json.dumps(
                {
                    "id": "local-response",
                    "object": "chat.completion",
                    "created": 0,
                    "model": payload.get("model", "local"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
            ).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class VerifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), JudgeHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.output = self.base / "output"
        self.output.mkdir()
        self.tests = self.base / "tests"
        self.tests.mkdir()
        shutil.copyfile(ORIGINAL_JUDGE, self.tests / "judge.py")
        self.logs = self.base / "logs"
        JudgeHandler.response_mode = "valid"
        JudgeHandler.requests = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_rubrics(self, rubrics: list[dict]) -> None:
        (self.tests / "RUBRICS.json").write_text(
            json.dumps({"rubrics": rubrics}), encoding="utf-8"
        )

    def run_verifier(self, *, with_key: bool = True) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "JUDGE_MODEL": "local-model",
            "JUDGE_API_BASE": f"http://127.0.0.1:{self.server.server_port}/v1",
            "JUDGE_API_KEY": "local-test-key" if with_key else "",
            "JUDGE_MAX_WORKERS": "1",
            "JUDGE_MAX_RETRIES": "1",
            "JUDGE_TIMEOUT_PER_RUBRIC": "5",
        }
        return subprocess.run(
            [
                sys.executable,
                str(VERIFY),
                "--output-dir",
                str(self.output),
                "--rubrics-file",
                str(self.tests / "RUBRICS.json"),
                "--judge-path",
                str(self.tests / "judge.py"),
                "--logs-dir",
                str(self.logs),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def reward(self) -> dict:
        return json.loads((self.logs / "reward.json").read_text())

    def test_real_judge_preserves_weighted_normalized_reward_and_reports(self) -> None:
        (self.output / "output.txt").write_text("finished", encoding="utf-8")
        self.write_rubrics(
            [
                {"rubric": "pass-rubric", "weight": 2, "criterion": ["one"]},
                {"rubric": "fail-rubric", "weight": 3, "criterion": ["one"]},
            ]
        )

        result = self.run_verifier()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.reward(), {"reward": 0.4})
        details = json.loads((self.logs / "judge-details.json").read_text())
        self.assertEqual(details["total_score"], 2)
        self.assertEqual(details["max_score"], 5)
        rubric_logs = sorted((self.logs / "rubrics").glob("*.log"))
        self.assertEqual(len(rubric_logs), 2)
        for log in rubric_logs:
            content = log.read_text()
            self.assertIn("Parse Status: direct_json", content)
            self.assertIn("API Exit Code: 0", content)
        report = json.loads((self.logs / "verification.json").read_text())
        self.assertEqual(report["status"], "ok")

    def test_empty_output_is_valid_zero_without_api_key_or_request(self) -> None:
        self.write_rubrics(
            [{"rubric": "pass-rubric", "weight": 1, "criterion": ["one"]}]
        )

        result = self.run_verifier(with_key=False)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.reward(), {"reward": 0.0})
        self.assertEqual(JudgeHandler.requests, [])

    def test_published_wrapper_json_is_cross_user_readable(self) -> None:
        self.write_rubrics(
            [{"rubric": "pass-rubric", "weight": 1, "criterion": ["one"]}]
        )

        result = self.run_verifier(with_key=False)

        self.assertEqual(result.returncode, 0, result.stderr)
        for path in (self.logs / "reward.json", self.logs / "verification.json"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644, path)

    def test_missing_key_with_output_is_infrastructure_error_without_reward(self) -> None:
        (self.output / "output.txt").write_text("finished", encoding="utf-8")
        self.write_rubrics(
            [{"rubric": "pass-rubric", "weight": 1, "criterion": ["one"]}]
        )

        result = self.run_verifier(with_key=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.logs / "reward.json").exists())
        report = json.loads((self.logs / "verification.json").read_text())
        self.assertEqual(report["status"], "error")
        self.assertIn("API key", report["error"])

    def test_api_failure_is_infrastructure_error_without_reward(self) -> None:
        JudgeHandler.response_mode = "api-error"
        (self.output / "output.txt").write_text("finished", encoding="utf-8")
        self.write_rubrics(
            [{"rubric": "pass-rubric", "weight": 1, "criterion": ["one"]}]
        )

        result = self.run_verifier()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.logs / "reward.json").exists())
        rubric_log = next((self.logs / "rubrics").glob("*.log")).read_text()
        self.assertIn("API Exit Code: 2", rubric_log)
        self.assertIn("Parse Status: failed", rubric_log)

    def test_malformed_response_is_infrastructure_error_without_reward(self) -> None:
        JudgeHandler.response_mode = "malformed"
        (self.output / "output.txt").write_text("finished", encoding="utf-8")
        self.write_rubrics(
            [{"rubric": "pass-rubric", "weight": 1, "criterion": ["one"]}]
        )

        result = self.run_verifier()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.logs / "reward.json").exists())
        rubric_log = next((self.logs / "rubrics").glob("*.log")).read_text()
        self.assertIn("API Exit Code: 1", rubric_log)
        self.assertIn("Parse Status: failed", rubric_log)

    def test_schema_malformed_response_is_error_even_when_json_parses(self) -> None:
        JudgeHandler.response_mode = "schema-malformed"
        (self.output / "output.txt").write_text("finished", encoding="utf-8")
        self.write_rubrics(
            [{"rubric": "pass-rubric", "weight": 1, "criterion": ["one"]}]
        )

        result = self.run_verifier()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.logs / "reward.json").exists())
        report = json.loads((self.logs / "verification.json").read_text())
        self.assertIn("schema", report["error"].lower())

    def test_visual_rubric_keeps_original_judge_image_extraction(self) -> None:
        (self.output / "chart.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"test-image-bytes"
        )
        self.write_rubrics(
            [{"rubric": "pass-rubric plot", "weight": 1, "criterion": ["one"]}]
        )

        result = self.run_verifier()

        self.assertEqual(result.returncode, 0, result.stderr)
        user_content = JudgeHandler.requests[0]["messages"][1]["content"]
        self.assertIsInstance(user_content, list)
        image_items = [item for item in user_content if item["type"] == "image_url"]
        self.assertEqual(len(image_items), 1)
        self.assertTrue(image_items[0]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_symlink_output_is_rejected_before_judge(self) -> None:
        self.write_rubrics(
            [{"rubric": "pass-rubric", "weight": 1, "criterion": ["one"]}]
        )
        (self.output / "leak.txt").symlink_to(self.tests / "RUBRICS.json")

        result = self.run_verifier()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.logs / "reward.json").exists())
        self.assertEqual(JudgeHandler.requests, [])
        report = json.loads((self.logs / "verification.json").read_text())
        self.assertIn("symlink", report["error"].lower())


if __name__ == "__main__":
    unittest.main()
