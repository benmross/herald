"""Non-mail leads retain full evidence and get reconsidered after an edit."""
import json
import pathlib
import sqlite3
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "lib")]
from collectors import opportunities
from herald import db


class Leads(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(db.SCHEMA)
        self.addCleanup(self.con.close)

    def test_lead_is_complete_private_attributed_and_versioned(self):
        fid = db.put_fact(self.con, "club_reader", "opportunity_lead", external_id="one",
            ts=db.now(), title="Internship", body="The full announcement with a deadline.",
            data={"from": "Club #jobs", "url": "https://example.org/message", "private": True})
        opportunities.collect(self.con)
        row = self.con.execute("SELECT * FROM facts WHERE source='opportunities'").fetchone()
        data = json.loads(row["data"])
        self.assertEqual(data["source_fact"], fid)
        self.assertEqual(data["from"], "Club #jobs")
        self.assertTrue(data["private"])
        self.assertEqual(row["body"], "The full announcement with a deadline.")
        first = row["external_id"]
        opportunities.collect(self.con)
        self.assertEqual(self.con.execute("SELECT external_id FROM facts WHERE source='opportunities'").fetchone()[0], first)
        self.con.execute("UPDATE facts SET body='Corrected application deadline.' WHERE id=?", (fid,))
        opportunities.collect(self.con)
        self.assertNotEqual(self.con.execute("SELECT external_id FROM facts WHERE source='opportunities'").fetchone()[0], first)

    def test_deleted_lead_is_not_a_candidate(self):
        db.put_fact(self.con, "club_reader", "opportunity_lead", external_id="one", ts=db.now(),
                    body="Old posting", data={"deleted": True})
        self.assertEqual(opportunities._leads(self.con), [])


if __name__ == "__main__":
    unittest.main()
