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
import io
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


class ExtensionKinds(RedTier):
    """An extension's kind uses the same door and cannot widen it."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        (self.tmp / "red.py").write_text(
            "from herald import red\n"
            "def _render(p): return 'Post to the board?\\n\\n' + p.get('text', '')\n"
            "def _perform(p): return ('posted', f'board:{red.acting()}')\n"
            "KINDS = {'board.post': (_render, _perform),\n"
            "         'mail.send': (_render, _perform),\n"
            "         'other.post': (_render, _perform)}\n")
        ext = mock.Mock()
        ext.name, ext.path = "board", self.tmp
        from herald import extensions
        for target, attr, value in ((extensions, "enabled", lambda: [ext]),
                                    (red, "_EXTENSION_KINDS", None)):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_only_its_own_prefix_and_never_a_builtin(self):
        with contextlib.redirect_stderr(io.StringIO()):
            known = red.kinds()
        self.assertIn("board.post", known)
        self.assertNotIn("other.post", known)
        self.assertIs(known["mail.send"], red.KINDS["mail.send"])

    def test_it_acts_once_on_a_tap_and_knows_it_is_inside_one(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIn("hello", red.render("board.post", {"text": "hello"}))
            pending = self.add("pending", "board.post", {"text": "hello"})
            with self.assertRaises(PermissionError):
                red.execute(pending)
            i = self.add("approved", "board.post", {"text": "hello"})
            result = red.execute(i)
        self.assertEqual(result["ref"], f"board:{i}",
                         "the performer sees the approval it runs under")
        self.assertIsNone(red.acting(), "nothing is acting outside execute")
        self.assertEqual(self.actions()[-1]["approval_id"], i)


class PullRequests(RedTier):
    """github.pr pushes exactly the commit the user saw, or nothing."""

    PAYLOAD = {"repo": "org/proj", "base": "master", "head": "me:fix/1",
               "remote": "origin", "push_to": "me/proj", "branch": "fix/1",
               "sha": "a" * 40, "dir": "/src/proj", "host": "laptop",
               "title": "Fix the thing", "body": "Closes #1",
               "commits": ["aaaaaaa Fix the thing"], "files": ["M\tsrc/x.ts"],
               "stat": "1 file changed, 1 insertion(+)", "target": "org/proj"}

    def test_render_comes_from_the_payload(self):
        text = red.render("github.pr", self.PAYLOAD)
        for needle in ("org/proj", "master from me:fix/1", "aaaaaaaaaa",
                       "me/proj on laptop", "Fix the thing", "src/x.ts", "Closes #1"):
            self.assertIn(needle, text)

    def test_moved_branch_pushes_nothing(self):
        from herald import ghpr
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return "b" * 40 + "\n"

        i = self.add("approved", kind="github.pr", payload=self.PAYLOAD)
        with mock.patch.object(ghpr, "run", fake_run):
            with self.assertRaises(PermissionError):
                red.execute(i)
        self.assertEqual(calls, [["git", "rev-parse", "fix/1"]])
        self.assertEqual(self.state(i), "done", "a failed action still consumes the tap")
        self.assertEqual(self.actions(), [])

    def test_pushes_the_approved_commit_then_opens_the_pr(self):
        from herald import ghpr
        calls = []

        def fake_run(argv, **kw):
            calls.append((argv, kw))
            if argv[:2] == ["git", "rev-parse"]:
                return "a" * 40 + "\n"
            if argv[:3] == ["gh", "pr", "create"]:
                return "https://github.com/org/proj/pull/7\n"
            return ""

        i = self.add("approved", kind="github.pr", payload=self.PAYLOAD)
        with mock.patch.object(ghpr, "run", fake_run):
            result = red.execute(i)
        argvs = [c[0] for c in calls]
        self.assertIn(["git", "push", "origin", "a" * 40 + ":refs/heads/fix/1"], argvs)
        create = next(c for c in calls if c[0][:3] == ["gh", "pr", "create"])
        self.assertEqual(create[1]["stdin"], "Closes #1")
        self.assertEqual(create[1]["host"], "laptop")
        self.assertEqual(result["ref"], "https://github.com/org/proj/pull/7")
        self.assertEqual(self.actions()[-1]["approval_id"], i)


class ApprovalTopic(unittest.TestCase):
    """An approval asked from a Telegram turn lands in that turn's topic."""

    def ask(self, env: dict) -> dict:
        from herald import notify
        sent = {}

        def api(method, **kw):
            sent.update(kw)
            return {"ok": True}

        with mock.patch.dict("os.environ", env, clear=False), \
                mock.patch.object(notify, "_telegram_chat_id", lambda: -100), \
                mock.patch.object(notify, "_updates_thread_id", lambda: 5), \
                mock.patch.object(notify, "_telegram_api", api):
            self.assertTrue(notify.ask(1, "Open this pull request?"))
        return sent

    def test_own_topic(self):
        self.assertEqual(self.ask({"HERALD_TELEGRAM_TOPIC": "-100:1356"})
                         ["message_thread_id"], 1356)

    def test_no_turn_goes_to_updates(self):
        import os
        env = dict(os.environ)
        env.pop("HERALD_TELEGRAM_TOPIC", None)
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(self.ask({})["message_thread_id"], 5)

    def test_topic_in_another_chat_is_ignored(self):
        self.assertEqual(self.ask({"HERALD_TELEGRAM_TOPIC": "-999:1356"})
                         ["message_thread_id"], 5)


if __name__ == "__main__":
    unittest.main()
