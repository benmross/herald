"""Quizzes answered without a model: the checks, and the races that would
skip a question or double-count one."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import db, study  # noqa: E402

SET = {"title": "t", "area": "X", "items": [
    {"kind": "short", "prompt": "1+1", "accept": ["2"], "solution": "two"},
    {"kind": "choice", "prompt": "pick", "options": ["a", "b"], "answer": 1,
     "feedback": ["no", "yes"]},
    {"kind": "open", "prompt": "prove", "solution": "proof"},
]}


class FakeApi:
    def __init__(self):
        self.calls = []

    def __call__(self, method, **params):
        self.calls.append((method, params))
        return {"ok": True}


def fresh():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA)
    return con


class Normalise(unittest.TestCase):
    def test_phone_typing_matches(self):
        item = {"accept": ["A=F B=T C=F D=T E=T", "FTFTT"]}
        for typed in ("a=f, b=t, c=f, d=t, e=t", "F T F T T", "ftftt"):
            self.assertTrue(study.is_correct(item, typed), typed)
        self.assertFalse(study.is_correct(item, "FTFTF"))

    def test_logic_words_and_symbols(self):
        item = {"accept": ["p ∨ q"]}
        for typed in ("p or q", "p v q", "p|q", "P ∨ Q"):
            self.assertTrue(study.is_correct(item, typed), typed)
        self.assertFalse(study.is_correct(item, "p and q"))

    def test_validate_refuses_bad_answer_index(self):
        bad = json.loads(json.dumps(SET))
        bad["items"][1]["answer"] = 5
        with self.assertRaises(study.SetError):
            study.validate(bad)


class Flow(unittest.TestCase):
    def setUp(self):
        self.con, self.api = fresh(), FakeApi()
        self.sid = study.create(self.con, SET, -100, 7)
        study.start(self.api, self.con, self.sid)

    def test_whole_set_then_review(self):
        handled, review = study.on_text(self.api, self.con, -100, 7, "2")
        self.assertTrue(handled)
        self.assertIsNone(review)
        toast, review = study.on_callback(self.api, self.con, f"study:{self.sid}:1:c:1")
        self.assertEqual(toast, "Right")
        handled, _ = study.on_text(self.api, self.con, -100, 7, "my proof")
        self.assertTrue(handled)
        # While waiting on a rating, typing is conversation, not an answer.
        self.assertEqual(study.on_text(self.api, self.con, -100, 7, "hm?"), (False, None))
        _, review = study.on_callback(self.api, self.con, f"study:{self.sid}:2:r:p")
        self.assertEqual(review["kind"], "review")
        self.assertIn("my proof", review["prompt"])
        self.assertIn("Self-rating: partial", review["prompt"])
        self.assertIsNone(study.active_for(self.con, -100, 7))

    def test_double_tap_advances_once(self):
        study.on_text(self.api, self.con, -100, 7, "2")
        study.on_callback(self.api, self.con, f"study:{self.sid}:1:c:0")
        toast, _ = study.on_callback(self.api, self.con, f"study:{self.sid}:1:c:1")
        self.assertEqual(toast, "Already answered")
        row = self.con.execute("SELECT pos FROM study_sets WHERE id = ?", (self.sid,)).fetchone()
        self.assertEqual(row["pos"], 2)

    def test_other_topic_is_untouched(self):
        self.assertEqual(study.on_text(self.api, self.con, -100, 8, "2"), (False, None))

    def test_commands_pass_through(self):
        self.assertEqual(study.on_text(self.api, self.con, -100, 7, "/new"), (False, None))


if __name__ == "__main__":
    unittest.main()
