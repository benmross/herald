"""Ledger output must never shorten a value without saying where the rest is.

A silent 60-character cut in `herald db` once turned a one-sentence answer into
five extra round trips. These pin the replacement: whole values, a stop at a row
boundary that names how to continue, and a file for anything no budget fits.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import factview  # noqa: E402


class WholeValues(unittest.TestCase):
    def test_one_value_prints_bare(self):
        self.assertEqual(factview.render([{"id": 301}]), "301")

    def test_no_rows(self):
        self.assertEqual(factview.render([]), "(no rows)")

    def test_narrow_rows_are_a_table(self):
        out = factview.render([{"id": 1, "title": "a"}, {"id": 2, "title": "b"}])
        self.assertIn("title", out.splitlines()[0])
        self.assertTrue(out.endswith("(2 rows)"))

    def test_a_long_value_is_printed_whole(self):
        body = "There will be no lab on Tuesday. " * 30
        out = factview.render([{"id": 7, "body": body}, {"id": 8, "body": "x"}])
        self.assertIn(body, out)
        self.assertIn("--- row 1 of 2 ---", out)

    def test_data_json_is_indented(self):
        out = factview.render([{"id": 1, "data": json.dumps({"from": "a", "to": "b"})}])
        self.assertIn('\n  "from": "a"', out)


class BoundedOutput(unittest.TestCase):
    def test_stops_at_a_row_boundary_and_says_how_to_continue(self):
        rows = [{"id": i, "body": "y" * 900} for i in range(10)]
        out = factview.render(rows, budget=3000)
        self.assertLessEqual(len(out), 3000 + 400)
        self.assertIn("stopped after", out)
        self.assertIn("herald fact", out)
        self.assertNotIn("y" * 901, out)

    def test_zero_budget_means_everything(self):
        rows = [{"id": i, "body": "y" * 900} for i in range(10)]
        self.assertNotIn("stopped after", factview.render(rows, budget=0))

    def test_an_oversized_first_row_goes_to_a_file_whole(self):
        body = "".join(f"line {i}\n" for i in range(20_000))
        with tempfile.TemporaryDirectory() as tmp:
            out = factview.render([{"id": 42, "body": body, "title": "t"}],
                                  budget=5000, spill_dir=pathlib.Path(tmp))
            path = pathlib.Path(tmp) / "fact-42-body.txt"
            self.assertEqual(path.read_text(), body)
            self.assertIn(str(path), out)
            self.assertLess(len(out), 5000)

    def test_an_oversized_scalar_goes_to_a_file_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = factview.render([{"body": "z" * 50_000}], budget=1000,
                                  spill_dir=pathlib.Path(tmp))
            files = list(pathlib.Path(tmp).iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_text(), "z" * 50_000)
            self.assertIn(str(files[0]), out)


if __name__ == "__main__":
    unittest.main()
