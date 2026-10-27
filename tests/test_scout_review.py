"""A displayed posting needs a reason before it disappears from Scout."""
import pathlib
import sqlite3
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lib"))
from cycles import scout
from herald import db


class Reviews(unittest.TestCase):
    def test_only_explicit_settled_decisions_are_remembered(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(db.SCHEMA)
        for key in ("accepted", "rejected", "deferred", "missing", "empty"):
            db.put_fact(con, "internships", "posting", external_id=key, title=key)
        shown = {"accepted", "rejected", "deferred", "missing", "empty", "mail"}
        decisions = [
            {"review_id": "accepted", "decision": "tracked", "reason": "Existing opportunity."},
            {"review_id": "rejected", "decision": "rejected", "reason": "Requires a completed doctorate."},
            {"review_id": "deferred", "decision": "deferred", "reason": "Posting unavailable."},
            {"review_id": "empty", "decision": "rejected", "reason": " "},
            {"review_id": "unshown", "decision": "rejected", "reason": "Invalid."},
        ]
        self.assertEqual(scout.reviewed_ids(con, shown, decisions), {"mail", "accepted", "rejected"})
        rows = con.execute("select external_id,body from facts where source='scout_review'").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["body"] for row in rows))


if __name__ == "__main__":
    unittest.main()
