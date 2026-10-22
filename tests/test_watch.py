"""Watches (22 Oct 2026): a watched page that starts matching is reported once,
a page that only churns numbers is not a change, and finished rows stop."""

from __future__ import annotations

import pathlib
import sqlite3
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import db, watch  # noqa: E402


def _con():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA)
    con.execute("INSERT INTO opportunities (id, found_at, title, org, status, watch_url, watch_for)"
                " VALUES (1, 'now', 'Ignite', 'NVIDIA', 'new', 'https://x', '2028 Ignite')")
    return con


class Watch(unittest.TestCase):
    def check(self, con, text):
        row = con.execute("SELECT * FROM opportunities WHERE id=1").fetchone()
        return watch.check(con, row, lambda u: (u, text))["event"]

    def test_hit_once(self):
        con = _con()
        self.assertEqual(self.check(con, "No results for ignite. 142 jobs"), "first")
        self.assertEqual(self.check(con, "No results for ignite. 143 jobs"), "same")
        self.assertEqual(self.check(con, "NVIDIA 2028 Ignite Internships: Software"), "hit")
        self.assertEqual(self.check(con, "NVIDIA 2028 Ignite Internships: Software"), "same")
        n = con.execute("SELECT COUNT(*) FROM facts WHERE source='watch' AND kind='hit'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_change_without_pattern(self):
        con = _con()
        con.execute("UPDATE opportunities SET watch_for=NULL")
        self.check(con, "Programs: none yet")
        self.assertEqual(self.check(con, "Programs: Ignite applications open"), "changed")

    def test_finished_rows_not_due(self):
        con = _con()
        self.assertEqual(len(watch.due(con)), 1)
        con.execute("UPDATE opportunities SET status='expired'")
        self.assertEqual(len(watch.due(con)), 0)

    def test_fetch_error_recorded(self):
        con = _con()
        row = con.execute("SELECT * FROM opportunities WHERE id=1").fetchone()

        def boom(u):
            raise TimeoutError("slow")
        self.assertEqual(watch.check(con, row, boom)["event"], "error")
        self.assertIn("slow", con.execute("SELECT watch_error FROM opportunities").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
