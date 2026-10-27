"""Applications (23 Oct 2026): the answer bank answers a question once and
reuses it, picks options by meaning, types words rather than patterns into
free-text boxes, and never reads a password back out of a page."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import apply as ap  # noqa: E402
from herald import ats, db  # noqa: E402

BANK = {"profile": {"first_name": "Ada", "school": "Example University.*North|Example U"},
        "answers": [
            {"q": "authori[sz]ed to work", "a": "^Yes"},
            {"q": "why do you want", "a": "Because of the maps.", "scope": "Acme"},
            {"q": "cover letter", "a": None},
            {"q": "^date$", "a": "@today"}]}


class Bank(unittest.TestCase):
    def test_lookup_order_and_scope(self):
        self.assertEqual(ap.lookup(BANK, "Are you authorized to work in the US?")[0], "^Yes")
        self.assertEqual(ap.lookup(BANK, "Why do you want to join?", "Acme Corp")[0], "Because of the maps.")
        self.assertIsNone(ap.lookup(BANK, "Why do you want to join?", "Other Inc")[0])
        self.assertTrue(ap.has_answer(BANK, "Cover Letter"))           # blank on purpose
        app = {ap.norm("Why do you want to join?"): "App-specific"}
        self.assertEqual(ap.lookup(BANK, "Why do you want to join?", "Acme", app)[0], "App-specific")
        self.assertRegex(ap.lookup(BANK, "Date")[0], r"^\d\d/\d\d/\d{4}$")

    def test_best_option(self):
        opts = ["Select One", "Yes", "No"]
        self.assertEqual(ap.best_option("^No", opts), "No")
        self.assertEqual(ap.best_option("not a protected veteran",
                                        ["I identify as a protected veteran", "I am not a protected veteran"]),
                         "I am not a protected veteran")
        self.assertIsNone(ap.best_option("Reston", ["Austin, TX", "Seattle, WA"]))

    def test_plain_text_from_pattern(self):
        self.assertEqual(ats.plain("Example University.*North|Example U"), "Example University, North")
        self.assertEqual(ats.plain("^Yes"), "Yes")
        self.assertEqual(ats.plain("Plain words, kept"), "Plain words, kept")

    def test_profile_rules(self):
        self.assertEqual(ats.profile_value(BANK, "First Name*"), "Ada")
        self.assertIsNone(ats.profile_value(BANK, "Middle name"))


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.p = mock.patch.object(ap, "bank_path", lambda: self.tmp / "answers.json")
        self.p.start()
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(db.SCHEMA)

    def tearDown(self):
        self.p.stop()

    def test_bank_and_application_answers(self):
        app = ap.record(self.con, url="https://jobs.example/1", company="Acme", ats="greenhouse")
        self.assertEqual(ap.store_answer(self.con, label="Why Acme?", value="Maps.", app_id=app), "application")
        self.assertEqual(ap.app_answers(ap.get(self.con, app)), {"why acme": "Maps."})
        self.assertEqual(ap.store_answer(self.con, label="Visa status", value="None", pattern="visa"), "bank")
        bank = json.loads((self.tmp / "answers.json").read_text())
        self.assertEqual(bank["answers"][0]["q"], "visa")
        ap.store_answer(self.con, label="Visa status", value="Still none", pattern="visa")
        self.assertEqual(len(json.loads((self.tmp / "answers.json").read_text())["answers"]), 1)

    def test_status_moves_opportunity(self):
        self.con.execute("INSERT INTO opportunities (id, found_at, title, org, status) VALUES (7,'x','T','O','new')")
        app = ap.record(self.con, url="https://jobs.example/2", opportunity=7)
        ap.set_state(self.con, app, status="submitted")
        self.assertEqual(self.con.execute("SELECT status FROM opportunities WHERE id=7").fetchone()[0], "applied")


class NoPasswords(unittest.TestCase):
    def test_choices_must_be_unambiguous(self):
        self.assertIsNone(ap.best_option('Computer Science',['Computer Science and Engineering','Computer Science and Mathematics']))
        self.assertIsNone(ap.best_option('No',['Not specified','Not applicable']))
        self.assertEqual(ap.best_option('^No$',['Yes','No']),'No')

    def test_canonical_requisition_deduplication(self):
        self.assertEqual(ap.canonical_key('https://example.wd5.myworkdayjobs.com/en-US/Jobs/job/City/Intern_R123'),ap.canonical_key('https://example.wd5.myworkdayjobs.com/en-US/Jobs/job/City/Intern_R123/apply/applyManually'))

    def test_scanners_mask_passwords(self):
        for js in (ats.SCAN_JS, ats.WD_SCAN):
            self.assertIn("'(set)'", js)


if __name__ == "__main__":
    unittest.main()


class MailStatus(unittest.TestCase):
    def test_confirmation_then_rejection(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("appcol", ROOT / "collectors" / "applications.py")
        col = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(col)
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(db.SCHEMA)
        app = ap.record(con, url="https://jobs.example/9", company="Acme Robotics")
        con.execute("UPDATE applications SET created_at='2026-10-01T00:00:00' WHERE id=?", (app,))
        db.put_fact(con, "gmail", "message", external_id="a", ts="2026-10-02T09:00:00",
                    title="Thank you for applying to Acme Robotics", body="We have received your application.",
                    data={"from": "no-reply@acmerobotics.com"})
        db.put_fact(con, "gmail", "message", external_id="b", ts="2026-10-02T09:00:00",
                    title="Thank you for applying to Other Co", body="received", data={"from": "x@other.com"})
        self.assertEqual(col.collect(con)["submitted"], 1)
        self.assertEqual(ap.get(con, app)["status"], "submitted")
        db.put_fact(con, "gmail", "message", external_id="c", ts="2026-10-09T09:00:00",
                    title="Your application to Acme Robotics",
                    body="Unfortunately we have decided not to move forward.", data={"from": "jobs@acmerobotics.com"})
        self.assertEqual(col.collect(con)["rejected"], 1)
        self.assertEqual(ap.get(con, app)["status"], "rejected")
