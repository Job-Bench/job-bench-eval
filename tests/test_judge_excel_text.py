"""Compact spreadsheet evidence preserves cell positions and formula meaning."""

import csv
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
from jobbench_eval.excel_text import format_sheets


def numeric_rows(text):
    return [row for row in csv.reader(text.splitlines()) if row and row[0].isdigit()]


class ExcelTextTests(unittest.TestCase):
    def test_eight_thousand_blank_formula_rows_fit_and_keep_dashboard(self):
        records = [
            {"cell": f"B{row}", "value": None, "formula": f'=IF(A{row}="","",A{row}*2)'}
            for row in range(2, 8002)
        ]
        result = {"cells": {"Donors": {
            record["cell"]: {"value": "", "display": "", "error_code": 0}
            for record in records
        }}}

        text = format_sheets([
            ("Donors", records),
            ("Dashboard", [{"cell": "A1", "value": "Approved gifts"}, {"cell": "B1", "value": 73}]),
        ], result)

        self.assertLess(len(text), 190000)
        self.assertIn("B2:B8001", text)
        self.assertIn("8000", text)
        self.assertIn('=IF(A2=', text)
        self.assertIn("Dashboard", text)
        self.assertIn(["1", "Approved gifts", "73"], numeric_rows(text))
        self.assertNotIn("[EXCEL TEXT TRUNCATED", text)
        self.assertEqual(len(numeric_rows(text)), 1)

    def test_formula_families_require_exact_fill_down_references_and_contiguity(self):
        records = [
            {"cell": "B2", "value": 0, "formula": "=$A2+C$1+$D$1"},
            {"cell": "B3", "value": 0, "formula": "=$A3+C$1+$D$1"},
            {"cell": "B4", "value": 0, "formula": "=$A4+D$1+$D$1"},
            {"cell": "B5", "value": 0, "formula": "=$A5+D$1+$D$1"},
            {"cell": "B7", "value": 0, "formula": "=$A7+D$1+$D$1"},
            {"cell": "C2", "value": 0, "formula": "=$A2+C$1+$D$1"},
        ]

        text = format_sheets([("References", records)], None)

        self.assertIn("B2:B3", text)
        self.assertIn("B4:B5", text)
        self.assertNotIn("B2:B5", text)
        self.assertNotIn("B4:B7", text)
        self.assertIn("=$A2+C$1+$D$1", text)
        self.assertIn("=$A4+D$1+$D$1", text)
        self.assertIn("=$A7+D$1+$D$1", text)
        self.assertIn("C2", text)

    def test_values_grid_uses_recalculation_and_keeps_errors_distinct_from_zero(self):
        records = [
            {"cell": "A1", "value": 0, "formula": "=1/0"},
            {"cell": "B1", "value": None, "formula": "=1-1"},
            {"cell": "D1", "value": 17, "formula": "=5*2"},
            {"cell": "D2", "value": False},
        ]
        result = {"cells": {"Numbers": {
            "A1": {"value": 0, "display": "#DIV/0!", "error_code": 532},
            "B1": {"value": 0, "display": "0", "error_code": 0},
            "D1": {"value": 10, "display": "10", "error_code": 0},
        }}}

        text = format_sheets([("Numbers", records)], result)

        rows = numeric_rows(text)
        self.assertIn("row,A,B,D", text)
        first = next(row for row in rows if row[0] == "1")
        self.assertIn("#DIV/0!", first[1])
        self.assertIn("532", first[1])
        self.assertEqual(first[2:], ["0", "10"])
        self.assertIn("cached=null", text)
        self.assertIn("cached=17", text)
        self.assertIn("recalculated=10", text)
        self.assertIn(["2", "", "", "False"], rows)

    def test_small_formula_evidence_preserves_zero_null_and_original_formulas(self):
        records = [
            {"cell": "C4", "value": None, "formula": "=A4+B4"},
            {"cell": "C5", "value": 0, "formula": "=A5+B5"},
            {"cell": "C7", "value": "#N/A", "cell_error": True},
        ]

        text = format_sheets([("Uncalculated", records)], None)

        for expected in ("C4:C5", "=A4+B4", "cached=null", "cached=0", "#N/A"):
            self.assertIn(expected, text)
        self.assertIn(["5", "0"], numeric_rows(text))
        self.assertNotIn("recalculated=0", text)

    def test_array_formula_spill_range_is_not_mistaken_for_fill_down(self):
        records = [
            {"cell": "A1", "value": 1, "formula": "=ROW(A1:A3)", "range": "A1:A3"},
            {"cell": "A2", "value": 2, "formula": "=ROW(A2:A4)", "range": "A2:A4"},
        ]

        text = format_sheets([("Arrays", records)], None)

        self.assertIn("A1:A3", text)
        self.assertIn("A2:A4", text)
        self.assertNotIn("A1:A2:", text)
        self.assertIn("=ROW(A1:A3)", text)
        self.assertIn("=ROW(A2:A4)", text)

    def test_budget_preserves_each_sheet_and_head_tail_rows_with_explicit_omissions(self):
        sheets = [
            ("First data", [{"cell": f"A{row}", "value": f"first-{row}: " + "x" * 30} for row in range(1, 201)]),
            ("Second data", [{"cell": f"A{row}", "value": f"second-{row}: " + "y" * 30} for row in range(1, 201)]),
            ("Final dashboard", [{"cell": "A1", "value": "Net total"}, {"cell": "B1", "value": 4512}]),
        ]

        text = format_sheets(sheets, None, max_chars=1500)

        self.assertLessEqual(len(text), 1500)
        for expected in ("First data", "Second data", "Final dashboard", "first-1:", "first-200:",
                         "second-1:", "second-200:", "4512", "[EXCEL TEXT TRUNCATED"):
            self.assertIn(expected, text)
        self.assertNotIn("first-100:", text)
        self.assertNotIn("second-100:", text)

    def test_small_per_sheet_budget_still_keeps_each_sheet_title(self):
        sheets = [(name, [{"cell": f"A{row}", "value": "long evidence " * 10}
                          for row in range(1, 20)])
                  for name in ("Alpha", "Beta", "Dashboard")]

        text = format_sheets(sheets, None, max_chars=400)

        self.assertLessEqual(len(text), 400)
        for name in ("Alpha", "Beta", "Dashboard"):
            self.assertIn(f"=== Sheet: {name} ===", text)
        self.assertIn("[EXCEL TEXT TRUNCATED", text)

    def test_large_family_keeps_middle_error_location_and_visible_error_value(self):
        records = [{"cell": f"B{row}", "value": 0, "formula": f'=IF(A{row}="","",1/A{row})'}
                   for row in range(1, 101)]
        values = {record["cell"]: {"value": "", "display": "", "error_code": 0}
                  for record in records}
        values["B50"] = {"value": 0, "display": "#DIV/0!", "error_code": 532}

        text = format_sheets([("Errors", records)], {"cells": {"Errors": values}})

        self.assertIn("B1:B100", text)
        self.assertIn("B50", text)
        self.assertIn("errors=1", text)
        row = next(row for row in numeric_rows(text) if row[0] == "50")
        self.assertIn("#DIV/0!", row[1])
        self.assertIn("532", row[1])

    def test_large_date_formula_family_keeps_formatted_dates_in_value_grid(self):
        records = [{"cell": f"B{row}", "value": None, "formula": f"=A{row}+1", "date_format": True}
                   for row in range(1, 41)]
        values = {record["cell"]: {"value": 46277.0, "display": "2026-09-12", "error_code": 0}
                  for record in records}
        records.append({"cell": "C1", "value": 0, "formula": "=46277"})
        values["C1"] = {"value": 46277.0, "display": "46,277", "error_code": 0}

        text = format_sheets([("Schedule", records)], {"cells": {"Schedule": values}})

        rows = numeric_rows(text)
        self.assertIn("B1:B40", text)
        self.assertIn(["1", "2026-09-12", "46277.0"], rows)
        self.assertIn(["40", "2026-09-12", ""], rows)
        self.assertEqual(values["B40"]["value"], 46277.0)


if __name__ == "__main__":
    unittest.main()
