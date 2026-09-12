"""Offline regressions for trailing commas in saved judge responses."""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
import judge

SPEC = importlib.util.spec_from_file_location("recovery_verifier", ROOT / "harbor/runtime/verify.py")
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


class JudgeJSONRecoveryTests(unittest.TestCase):
    def payload(self):
        return {
            "criteria_results": [{"index": 0, "passed": True,
                                  "reasoning": 'Preserve ,] and ,}, quotes " and backslashes \\.',
                                  "evidence": "Unicode 東京; literal [,] and {,}."}],
            "rubric_passed": True,
            "overall_reasoning": "One criterion passed.",
        }

    def assert_parity(self, raw, expected, expected_status):
        actual, status = judge.parse_judge_json(raw)
        self.assertEqual(status, expected_status)
        self.assertEqual(actual, expected)
        self.assertIn(status, verifier.SUCCESSFUL_PARSE_STATUSES)
        self.assertEqual(verifier._parse_judge_response(raw, status), expected)

    def test_nested_array_and_object_trailing_commas(self):
        raw = '{"criteria_results": [{"nested": [1, false, null,], "object": {"value": true,},},],}'
        self.assert_parity(raw, {"criteria_results": [{"nested": [1, False, None],
                                                     "object": {"value": True}}]},
                           "direct_json_trailing_commas")

    def test_preserves_strings_escaped_quotes_backslashes_and_unicode(self):
        expected = self.payload()
        expected["criteria_results"][0]["evidence"] += '\\" end \\\\ '
        raw = json.dumps(expected, ensure_ascii=False)[:-1] + ",\t\r\n}"
        self.assert_parity(raw, expected, "direct_json_trailing_commas")

    def test_valid_json_and_existing_fallback_statuses_unchanged(self):
        expected = self.payload()
        raw = json.dumps(expected)
        cases = [
            (raw, "direct_json"),
            ("```json\n" + raw + "\n```", "markdown_fence"),
            ("Some prose\n" + raw + "\nMore prose", "first_last_brace"),
        ]
        for wrapped, status in cases:
            with self.subTest(status=status):
                self.assert_parity(wrapped, expected, status)
        self.assert_parity('{"criteria_results": []} between {"criteria_results": [true]}',
                           {"criteria_results": [True]}, "regex_extract")
        for valid in ("[]", "{}", "false", "null", '"literal ,] ,}"'):
            with self.subTest(valid=valid):
                self.assertEqual(judge.parse_judge_json(valid), (json.loads(valid), "direct_json"))

    def test_recovery_preserves_fence_brace_and_regex_extraction(self):
        expected = self.payload()
        raw = json.dumps(expected)[:-1] + ",}"
        cases = [
            ("```json\n" + raw + "\n```", "markdown_fence_trailing_commas"),
            ("Some prose\n" + raw + "\nMore prose", "first_last_brace_trailing_commas"),
        ]
        for wrapped, status in cases:
            with self.subTest(status=status):
                self.assert_parity(wrapped, expected, status)
        self.assert_parity('{"criteria_results": [,]} between {"criteria_results": [true,],}',
                           {"criteria_results": [True]}, "regex_extract_trailing_commas")

    def test_existing_valid_fallback_takes_precedence_over_recovery(self):
        raw = '```json\n{"criteria_results": [1,]}\n```\n{"criteria_results": []}'
        self.assert_parity(raw, {"criteria_results": []}, "regex_extract")

    def test_rejects_missing_values_multiple_commas_and_other_invalid_syntax(self):
        invalid = [
            "[,]", "{,}", "[1,,]", "[1,,,]", '{"criteria_results": [,]}',
            '{"criteria_results": [1,,]}', '{"criteria_results": [1,],,}',
            '{"criteria_results": [1,], "missing":,}',
            '{"criteria_results": [1,], "passed": true/false}',
            '{"criteria_results": [1,\v]}', '{"criteria_results": [1,\u00a0]}',
            '{"criteria_results": [1,] /* comment */}',
            '{"criteria_results": [\'single quotes\',]}',
            '{"criteria_results": ["bad\\q",]}',
            '{"criteria_results": ["unterminated,],}',
            '{"criteria_results": [1,}}',
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    judge.parse_judge_json(raw)
                with self.assertRaises(verifier.VerificationError):
                    verifier._parse_judge_response(raw, "direct_json_trailing_commas")

    def test_judge_keeps_original_raw_response_and_verifier_accepts_saved_log(self):
        raw = json.dumps(self.payload())[:-1] + ",}"
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=raw))])
        create = unittest.mock.Mock(return_value=response)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(judge, "get_openai_client", return_value=client):
            result, debug = judge.judge_rubric(0, {"rubric": "One requirement", "weight": 7,
                "criterion": ["One criterion"]}, "saved output", "offline", "unused", "unused", 1)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(result["result"]["score"], 7)
        self.assertEqual(debug["raw_response"], raw)
        self.assertEqual(debug["parse_status"], "direct_json_trailing_commas")
        self.assertEqual(debug["api_exit_code"], 0)
        with tempfile.TemporaryDirectory() as temporary:
            logs = Path(temporary)
            judge.write_detail_log(logs, "jobbench", 0, "offline", debug, result)
            self.assertIn(raw, (logs / "jobbench_rubric_0.log").read_text())
            verifier._validate_judge_logs(logs, 1)

    def test_recovery_does_not_bypass_verifier_schema_validation(self):
        raw = '{"criteria_results": [], "rubric_passed": true, "overall_reasoning": "empty",}'
        with tempfile.TemporaryDirectory() as temporary:
            logs = Path(temporary)
            judge.write_detail_log(logs, "jobbench", 0, "offline", {
                "parse_status": "direct_json_trailing_commas", "api_exit_code": 0,
                "criterion_count": 1, "raw_response": raw,
            }, {})
            with self.assertRaisesRegex(verifier.VerificationError, "schema is invalid"):
                verifier._validate_judge_logs(logs, 1)


if __name__ == "__main__":
    unittest.main()
