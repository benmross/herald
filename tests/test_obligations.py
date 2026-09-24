"""Obligations and deadlines: the two halves of what the commitments table
used to be (23 Sep 2026).

What must hold: a quiz marks the class meeting it happens in and the meeting
goes back to exactly what it was when the quiz goes away; a new obligation is
shown to the user once and never twice; a tap closes it.
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import db, deadlines, obligations  # noqa: E402

CFG = {"prefix": {"quiz": "QUIZ", "test": "TEST", "due": "DUE", "event": ""},
       "colors": {"quiz": "5", "test": "11", "due": "3", "event": ""}}


def _meeting(eid, summary, start, private=None, color=""):
    ev = {"id": eid, "summary": summary, "start": {"dateTime": start}, "colorId": color}
    if private:
        ev["extendedProperties"] = {"private": private}
    return ev


class Deadlines(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(deadlines, "_cfg", return_value=CFG)
        p.start()
        self.addCleanup(p.stop)

    def test_classify(self):
        self.assertEqual(deadlines.classify("Quiz 3"), "quiz")
        self.assertEqual(deadlines.classify("Exam #1"), "test")
        self.assertEqual(deadlines.classify("Final exam"), "test")
        self.assertEqual(deadlines.classify("P3"), "due")

    def test_meeting_nearest_the_time(self):
        lec = _meeting("a", "CMSC132L-IRB0324", "2026-10-07T14:00:00-04:00")
        dis = _meeting("b", "CMSC132D-CSI3120", "2026-10-07T08:00:00-04:00")
        other = _meeting("c", "MATH240L-SKN0200", "2026-10-07T13:00:00-04:00")
        row = {"course": "CMSC132", "due": "2026-10-07T13:00:00-04:00"}
        self.assertEqual(deadlines.meeting_for(row, [lec, dis, other])["id"], "a")
        self.assertIsNone(deadlines.meeting_for(
            {"course": "CMSC132", "due": "2026-10-08"}, [lec, dis]))

    def test_tag_then_restore_round_trip(self):
        ev = _meeting("m", "MATH240D-MTH0405", "2026-09-24T09:00:00-04:00", color="")
        row = {"id": 7, "kind": "quiz", "title": "MATH240 Quiz 3"}
        (op, _, patch, _), = deadlines.plan_tags([ev], {"m": [row]})
        self.assertEqual(op, "tag")
        self.assertEqual(patch["summary"], "QUIZ: MATH240D-MTH0405")
        self.assertEqual(patch["colorId"], "5")
        # as Google would now hold it
        tagged = _meeting("m", patch["summary"], "2026-09-24T09:00:00-04:00",
                          patch["extendedProperties"]["private"], "5")
        self.assertEqual(deadlines.plan_tags([tagged], {"m": [row]}), [])
        (op, _, patch, _), = deadlines.plan_tags([tagged], {})
        self.assertEqual(op, "restore")
        self.assertEqual(patch["summary"], "MATH240D-MTH0405")
        self.assertIsNone(patch["colorId"])

    def test_test_outranks_quiz_on_one_meeting(self):
        ev = _meeting("m", "X101", "2026-09-24T09:00:00-04:00")
        rows = [{"id": 1, "kind": "quiz", "title": "q"}, {"id": 2, "kind": "test", "title": "t"}]
        (_, _, patch, _), = deadlines.plan_tags([ev], {"m": rows})
        self.assertTrue(patch["summary"].startswith("TEST: "))

    def test_hand_renamed_meeting_is_left_alone(self):
        ev = _meeting("m", "Study group instead", "2026-09-24T09:00:00-04:00",
                      {"heraldTag": "7", "heraldOrigSummary": "X101"})
        (op, *_), = deadlines.plan_tags([ev], {"m": [{"id": 7, "kind": "quiz", "title": "q"}]})
        self.assertEqual(op, "skip")


class Obligations(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(db.SCHEMA)
        self.sent = []
        p = mock.patch.object(obligations.notify, "buttons",
                              side_effect=lambda text, rows, **k: self.sent.append(text) or True)
        p.start()
        self.addCleanup(p.stop)

    def test_dated_course_item_is_refused(self):
        with self.assertRaises(ValueError):
            obligations.add(self.con, text="Quiz 3", kind="deadline")

    def test_announced_once(self):
        obligations.add(self.con, text="Reply to Sam", kind="unanswered")
        self.assertEqual(len(self.sent), 1)
        obligations.sweep(self.con)
        self.assertEqual(len(self.sent), 1)

    def test_silent_insert_is_caught_by_the_sweep(self):
        self.con.execute("INSERT INTO commitments (created_at, text, kind, status)"
                         " VALUES ('2026-09-01', 'x', 'owed', 'open')")
        obligations.sweep(self.con)
        self.assertEqual(len(self.sent), 1)

    def test_stale_undated_row_is_asked_about(self):
        self.con.execute("INSERT INTO commitments (created_at, text, kind, status,"
                         " announced_at) VALUES ('2026-01-01', 'x', 'owed', 'open',"
                         " '2026-01-01T00:00:00-05:00')")
        obligations.sweep(self.con)
        self.assertTrue(self.sent and self.sent[0].startswith("**Still real?"))
        obligations.sweep(self.con)
        self.assertEqual(len(self.sent), 1)

    def test_tap_closes(self):
        oid = obligations.add(self.con, text="x", kind="owed")
        toast, line = obligations.on_callback(self.con, f"oblig:{oid}:done")
        self.assertEqual(line, "Done.")
        st = self.con.execute("SELECT status FROM commitments WHERE id=?", (oid,)).fetchone()[0]
        self.assertEqual(st, "done")
        self.assertEqual(obligations.on_callback(self.con, f"oblig:{oid}:drop")[0], "Already done.")


if __name__ == "__main__":
    unittest.main()
