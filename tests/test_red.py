"""A red action happens once, only on a granted tap, and says what it will do.

`approvals.py` existed for months and never fired, so none of these properties
had ever been exercised. They are the whole value of the red tier, and each one
closes a specific way a rule-in-a-prompt fails:

- an approval that is pending, denied or expired does nothing
- one tap is one action, however many times execute is called
- a failed action consumes its approval, so an old tap cannot be replayed
- a red row in the audit table must point at a granted approval
- what the user is shown is built from the payload, not from the caller's words
"""

from __future__ import annotations

import contextlib
import json
import pathlib
import sqlite3
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import db, red  # noqa: E402

DDL = """
CREATE TABLE actions (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL,
    tier TEXT NOT NULL, kind TEXT NOT NULL, target TEXT, summary TEXT NOT NULL,
    ref TEXT, reported_at TEXT, approval_id INTEGER);
CREATE TABLE approvals (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL,
    kind TEXT NOT NULL, target TEXT, summary TEXT NOT NULL, payload TEXT,
    state TEXT NOT NULL, decided_at TEXT, decided_by TEXT);
"""


class RedTier(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(DDL)

        @contextlib.contextmanager
        def session():
            yield self.con

        def get(i):
            row = self.con.execute("SELECT * FROM approvals WHERE id = ?", (i,)).fetchone()
            return dict(row) if row else None

        for target, attr, value in ((red.db, "session", session),
                                    (red.approvals, "get", get)):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def add(self, state: str, kind: str = "drill", payload: dict | None = None) -> int:
        cur = self.con.execute(
            "INSERT INTO approvals (ts, actor, kind, summary, payload, state) "
            "VALUES ('now', 'test', ?, 's', ?, ?)", (kind, json.dumps(payload or {}), state))
        return cur.lastrowid

    def actions(self) -> list[dict]:
        return [dict(r) for r in self.con.execute("SELECT * FROM actions")]

    def state(self, i: int) -> str:
        return self.con.execute("SELECT state FROM approvals WHERE id = ?", (i,)).fetchone()[0]

    # -- only a granted tap acts ------------------------------------------------

    def test_nothing_happens_without_approval(self):
        for state in ("pending", "denied", "expired"):
            i = self.add(state)
            with self.assertRaises(PermissionError):
                red.execute(i)
            self.assertEqual(self.state(i), state, "a refused execute must not move the row")
        self.assertEqual(self.actions(), [])

    def test_a_missing_approval_is_refused(self):
        with self.assertRaises(PermissionError):
            red.execute(9999)

    # -- once -------------------------------------------------------------------

    def test_one_tap_is_one_action(self):
        i = self.add("approved")
        result = red.execute(i)
        self.assertEqual(result["kind"], "drill")
        with self.assertRaises(PermissionError):
            red.execute(i)
        rows = self.actions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tier"], "red")
        self.assertEqual(rows[0]["approval_id"], i)
        self.assertEqual(self.state(i), "done")

    def test_a_failed_action_consumes_the_approval(self):
        """Fail closed. Leaving it approved would let the same tap be replayed
        later by any process that calls execute."""
        i = self.add("approved")
        boom = (red.KINDS["drill"][0], mock.Mock(side_effect=RuntimeError("api down")))
        with mock.patch.dict(red.KINDS, {"drill": boom}):
            with self.assertRaises(RuntimeError):
                red.execute(i)
        self.assertEqual(self.state(i), "done")
        self.assertEqual(self.actions(), [])
        with self.assertRaises(PermissionError):
            red.execute(i)

    def test_an_unknown_kind_is_refused_even_if_approved(self):
        i = self.add("approved", kind="money.spend")
        with self.assertRaises(PermissionError):
            red.execute(i)
        self.assertEqual(self.actions(), [])

    # -- the audit table cannot be lied to -------------------------------------

    def test_a_red_row_without_an_approval_is_refused(self):
        with self.assertRaises(PermissionError):
            db.record_action(self.con, actor="t", tier="red", kind="mail.send", summary="s")

    def test_a_red_row_citing_an_ungranted_approval_is_refused(self):
        i = self.add("pending")
        with self.assertRaises(PermissionError):
            db.record_action(self.con, actor="t", tier="red", kind="mail.send",
                             summary="s", approval_id=i)

    def test_an_invented_tier_is_refused(self):
        with self.assertRaises(ValueError):
            db.record_action(self.con, actor="t", tier="orange", kind="x", summary="s")

    def test_amber_still_records_without_ceremony(self):
        db.record_action(self.con, actor="t", tier="amber", kind="calendar.create", summary="s")
        self.assertEqual(len(self.actions()), 1)

    # -- the user sees the real thing ------------------------------------------

    def test_the_tap_text_comes_from_the_payload(self):
        text = red.render("mail.send", {"to": "someone@example.com", "cc": "b@example.com",
                                        "subject": "Quarterly numbers",
                                        "body": "Attached are the numbers."})
        for part in ("someone@example.com", "b@example.com", "Quarterly numbers",
                     "Attached are the numbers."):
            self.assertIn(part, text)

    def test_a_long_body_is_previewed_not_hidden(self):
        text = red.render("mail.send", {"to": "a@example.com", "subject": "s",
                                        "body": "x" * 5000})
        self.assertIn("[...]", text)
        self.assertLess(len(text), 1000)

    def test_an_incomplete_mail_is_refused_before_anything_is_sent(self):
        i = self.add("approved", kind="mail.send", payload={"to": "a@example.com"})
        with self.assertRaises(ValueError):
            red.execute(i)
        self.assertEqual(self.actions(), [])


if __name__ == "__main__":
    unittest.main()
