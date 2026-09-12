"""The between-messages delta must stay silent when nothing happened.

This rides along on every single message, so two failure modes matter more
than anything it reports. If it fires on a quiet turn it is pure noise on every
message forever, which is how a useful signal gets ignored. And if it fires on
a *textual* change rather than a real one it will fire every time, because the
orientation card it replaces carries a generation timestamp and a "last fix N
minutes ago" -- both of which always change and neither of which is news. That
is the specific reason this compares row identifiers instead of diffing text.
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT))

from cycles import _snapshot  # noqa: E402


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE facts (id INTEGER PRIMARY KEY, source TEXT, kind TEXT,
                            ts TEXT, title TEXT, data TEXT);
        CREATE TABLE actions (id INTEGER PRIMARY KEY, kind TEXT, target TEXT,
                              summary TEXT);
        CREATE TABLE commitments (id INTEGER PRIMARY KEY, text TEXT, due TEXT,
                                  status TEXT);
    """)
    return con


class QuietTurnsCostNothing(unittest.TestCase):
    def test_first_turn_has_nothing_to_be_since(self):
        con = _db()
        text, fp = _snapshot.delta(con, None)
        self.assertIsNone(text)
        self.assertIn("fact_id", fp)

    def test_an_unchanged_world_reports_nothing(self):
        con = _db()
        _, fp = _snapshot.delta(con, None)
        text, fp2 = _snapshot.delta(con, fp)
        self.assertIsNone(text)

    def test_the_clock_alone_does_not_trigger_it(self):
        """The card's timestamp and location age change constantly. Neither is
        in the fingerprint, so neither can fire the delta."""
        con = _db()
        _, fp = _snapshot.delta(con, None)
        self.assertNotIn("generated", fp)
        self.assertNotIn("age_minutes", fp)
        con.execute("INSERT INTO facts (source, kind, ts, title, data) "
                    "VALUES ('dawarich','current','now','UMD campus',"
                    "'{\"age_minutes\": 4}')")
        # Same place, newer fix: a real row arrived, so the catch-all mentions
        # it, but the *place* is what the fingerprint tracks.
        text, fp2 = _snapshot.delta(con, fp)
        self.assertEqual(fp2["place"], "UMD campus")
        text2, fp3 = _snapshot.delta(con, fp2)
        self.assertIsNone(text2)


class RealChangesAreReported(unittest.TestCase):
    def test_new_mail_is_named(self):
        con = _db()
        _, fp = _snapshot.delta(con, None)
        con.execute("INSERT INTO facts (source, kind, ts, title, data) VALUES "
                    "('gmail','message','now','Exam moved',"
                    "'{\"from\": \"A Lecturer <lecturer@example.edu>\"}')")
        text, _ = _snapshot.delta(con, fp)
        self.assertIn("Exam moved", text)
        self.assertIn("A Lecturer", text)
        self.assertNotIn("lecturer@example.edu", text)

    def test_amber_writes_are_surfaced(self):
        """'Never silent' means a write Herald made mid-conversation reaches
        them at the first opportunity, which is the next message."""
        con = _db()
        _, fp = _snapshot.delta(con, None)
        con.execute("INSERT INTO actions (kind, target, summary) VALUES "
                    "('calendar.create','UMD','added a review session')")
        text, _ = _snapshot.delta(con, fp)
        self.assertIn("calendar.create", text)
        self.assertIn("added a review session", text)

    def test_a_new_commitment_is_reported_with_its_due_date(self):
        con = _db()
        _, fp = _snapshot.delta(con, None)
        con.execute("INSERT INTO commitments (text, due, status) VALUES "
                    "('Sign the form','2026-09-20','open')")
        text, _ = _snapshot.delta(con, fp)
        self.assertIn("Sign the form", text)
        self.assertIn("2026-09-20", text)

    def test_closing_a_commitment_is_not_read_as_opening_one(self):
        """open_n falls both when something closes and never when something
        opens, so the two have to be told apart by id rather than by count."""
        con = _db()
        con.execute("INSERT INTO commitments (text, due, status) VALUES "
                    "('old thing', NULL, 'open')")
        _, fp = _snapshot.delta(con, None)
        con.execute("UPDATE commitments SET status='done' WHERE text='old thing'")
        text, _ = _snapshot.delta(con, fp)
        self.assertIn("closed or dropped", text)
        self.assertNotIn("new commitment", text)

    def test_opening_one_is_not_read_as_closing_one(self):
        con = _db()
        _, fp = _snapshot.delta(con, None)
        con.execute("INSERT INTO commitments (text, due, status) VALUES "
                    "('new thing', NULL, 'open')")
        text, _ = _snapshot.delta(con, fp)
        self.assertIn("new thing", text)
        self.assertNotIn("closed or dropped", text)

    def test_long_subjects_are_clipped(self):
        con = _db()
        _, fp = _snapshot.delta(con, None)
        con.execute("INSERT INTO facts (source, kind, ts, title, data) VALUES "
                    "('gmail','message','now',?,'{\"from\": \"x\"}')",
                    ("word " * 200,))
        text, _ = _snapshot.delta(con, fp)
        self.assertTrue(all(len(line) < 140 for line in text.splitlines()),
                        "a mailing-list subject must not run away with the block")


if __name__ == "__main__":
    unittest.main()
