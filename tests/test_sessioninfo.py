"""/debug and `herald session` report what was recorded, without a model."""

from __future__ import annotations

import pathlib
import sqlite3
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import sessioninfo  # noqa: E402


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE runs (id INTEGER PRIMARY KEY, ts TEXT, label TEXT, model TEXT,
            session_id TEXT, duration_ms INT, cost_usd REAL, input_tokens INT,
            output_tokens INT, cache_read INT, cache_write INT, context_tokens INT,
            startup_ms INT, model_ms INT, tool_ms INT, round_trips INT, error TEXT);
        CREATE TABLE run_phases (id INTEGER PRIMARY KEY, run_id INT, seq INT,
            kind TEXT, name TEXT, ms INT, detail TEXT);
    """)
    for rid, ctx in ((10, 31000), (11, 49961)):
        con.execute("INSERT INTO runs VALUES (?, '2026-09-14T12:02:20-04:00', "
                    "'telegram', 'opus', 'abcdef12-0000', 42350, 0.25, 162, 2054, "
                    "282178, 5622, ?, 599, 36384, 7788, 13, NULL)", (rid, ctx))
    con.execute("INSERT INTO run_phases (run_id, seq, kind, name, ms, detail) "
                "VALUES (11, 1, 'tool', 'Bash', 12100, 'herald db ...'), "
                "(11, 2, 'model', 'model', 9000, NULL)")
    return con


class Report(unittest.TestCase):
    def test_reports_context_and_the_last_turn(self):
        out = sessioninfo.report(_db(), "abcdef12-0000")
        self.assertIn("2 turns", out)
        self.assertIn("49,961 tokens", out)
        self.assertIn("run 11", out)
        self.assertIn("13 round trips", out)
        self.assertIn("Bash  herald db", out)

    def test_latest_session(self):
        self.assertEqual(sessioninfo.latest_session(_db()), "abcdef12-0000")

    def test_no_session_yet_still_shows_a_running_turn(self):
        out = sessioninfo.report(_db(), None, running={"seconds": 63, "step": "Bash: x"})
        self.assertIn("a turn is running: 63.0s, now: Bash: x", out)
        self.assertIn("no session in this topic yet", out)


if __name__ == "__main__":
    unittest.main()
