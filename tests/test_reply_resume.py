"""Replying to a message from an earlier conversation resumes that conversation.

`/new` moves a topic to a fresh session. The old one is still resumable by the
engine, and these tests cover the record that lets a reply find it again.
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import pathlib
import sqlite3
import sys
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import db, tgsessions  # noqa: E402

CHAT, TOPIC = -100, 5
KEY = f"{CHAT}:{TOPIC}"


def _load():
    path = ROOT / "bin" / "herald-telegram"
    loader = importlib.machinery.SourceFileLoader("tg_reply_under_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class ReplyResume(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tg = _load()

    def setUp(self):
        self.con = sqlite3.connect(":memory:", check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(db.SCHEMA)

        @contextlib.contextmanager
        def session():
            with self.con:
                yield self.con

        self.next_id = 1000
        self.sent: list[str] = []
        self.calls: list[dict] = []
        self.sessions = iter(["sess-A", "sess-B", "sess-C"])

        def api(method, **kw):
            if method != "sendMessage":
                return {"ok": True, "result": True}
            self.next_id += 1
            self.sent.append(kw["text"])
            return {"ok": True, "result": {"message_id": self.next_id}}

        def think_(prompt, **kw):
            self.calls.append(kw)
            sid = kw["resume"] or next(self.sessions)
            return types.SimpleNamespace(
                ok=True, cancelled=False, text=f"answer from {sid}",
                session_id=sid, cost_usd=0.0, error=None)

        patches = [
            mock.patch.object(self.tg.config, "default_engine", return_value="claude"),
            mock.patch.object(self.tg.db, "session", session),
            mock.patch.object(self.tg, "api", api),
            mock.patch.object(self.tg.think, "think", think_),
            mock.patch.object(self.tg.config, "secret",
                              side_effect=lambda k, *a: 42 if k == "telegram.user_id" else 1),
            mock.patch.object(self.tg, "delta_for", return_value=""),
            mock.patch.object(self.tg, "orientation_for",
                              side_effect=lambda ts, fresh: ts.setdefault("orientation", "card")),
            mock.patch.object(self.tg.activity, "Turn",
                              lambda *a, **k: contextlib.nullcontext()),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        self.state = {"offset": 0, "threads": {}}
        self.mid = 0

    def say(self, text, reply_to=None):
        """One message from the user; returns its id."""
        self.mid += 1
        msg = {"message_id": self.mid, "message_thread_id": TOPIC, "text": text,
               "chat": {"id": CHAT, "type": "supergroup"}, "from": {"id": 42},
               # Telegram marks every plain message in a topic as a reply to
               # the message that created the topic.
               "reply_to_message": {"message_id": reply_to or TOPIC}}
        with mock.patch("builtins.print"):
            self.tg.handle({"update_id": self.mid, "message": msg}, self.state)
        return self.mid

    @property
    def pointer(self):
        return self.state["threads"][KEY]["session_id"]

    def test_a_reply_to_the_earlier_conversation_resumes_it(self):
        first = self.say("one")
        self.assertEqual(self.pointer, "sess-A")
        self.say("/new")
        self.say("two")
        self.assertEqual(self.pointer, "sess-B")

        self.say("back to this", reply_to=first)
        self.assertEqual(self.calls[-1]["resume"], "sess-A")
        self.assertEqual(self.pointer, "sess-A")
        self.assertTrue(any("Back in the conversation" in t for t in self.sent))

        # The topic stays there without further replies.
        self.say("and more")
        self.assertEqual(self.calls[-1]["resume"], "sess-A")

    def test_herald_s_own_reply_resumes_it_too(self):
        self.say("one")
        answer = self.next_id                 # the id of "answer from sess-A"
        self.say("/new")
        self.say("two")
        self.say("continue", reply_to=answer)
        self.assertEqual(self.calls[-1]["resume"], "sess-A")

    def test_the_new_command_itself_belongs_to_the_conversation_it_ended(self):
        self.say("one")
        new = self.say("/new")
        self.say("two")
        self.say("continue", reply_to=new)
        self.assertEqual(self.calls[-1]["resume"], "sess-A")

    def test_the_conversation_left_behind_is_resumable_the_same_way(self):
        first = self.say("one")
        self.say("/new")
        second = self.say("two")
        self.say("back", reply_to=first)
        self.say("and forward again", reply_to=second)
        self.assertEqual(self.calls[-1]["resume"], "sess-B")

    def test_a_reply_inside_the_current_conversation_is_an_ordinary_message(self):
        first = self.say("one")
        self.say("more on that", reply_to=first)
        self.assertEqual(self.calls[-1]["resume"], "sess-A")
        self.assertFalse(any("Back in the conversation" in t for t in self.sent))

    def test_a_reply_to_an_unrecorded_message_changes_nothing(self):
        self.say("one")
        self.say("about that digest", reply_to=999999)
        self.assertEqual(self.calls[-1]["resume"], "sess-A")

    def test_the_resumed_session_gets_its_own_engine_and_card_back(self):
        first = self.say("one")
        self.state["threads"][KEY]["orientation"] = "card A"
        self.say("again")                      # saves card A with sess-A
        self.say("/new")
        ts = self.state["threads"][KEY]
        ts.update(engine="codex", model="other", orientation="card B")
        ts["session_id"] = None
        self.say("two")
        self.say("back", reply_to=first)
        self.assertEqual(self.calls[-1]["engine"], "claude")
        self.assertEqual(ts["orientation"], "card A")

    def test_a_reply_meant_for_another_conversation_is_not_steered_into_the_running_one(self):
        first = self.say("one")
        self.say("/new")
        self.say("two")
        msg = {"message_id": 77, "message_thread_id": TOPIC, "text": "back",
               "chat": {"id": CHAT}, "from": {"id": 42},
               "reply_to_message": {"message_id": first}}
        with mock.patch.object(self.tg.think, "steer", return_value=True) as steer, \
                mock.patch("builtins.print"):
            self.assertFalse(self.tg._try_steer({"message": msg}, KEY, self.state))
        steer.assert_not_called()

    def test_a_session_from_another_topic_is_not_resumed(self):
        with self.con:
            tgsessions.save(self.con, "sess-X", f"{CHAT}:99", engine="claude",
                            model=None, turns=3, orientation=None)
            tgsessions.record(self.con, CHAT, [500], "sess-X")
        self.say("hello", reply_to=500)
        self.assertIsNone(self.calls[-1]["resume"])

    def test_cli_resume_usage_errors_do_not_abandon_the_transcript(self):
        self.assertFalse(self.tg.transcript_missing(
            "error: unexpected argument '--add-dir' found\nUsage: codex exec resume"))
        self.assertTrue(self.tg.transcript_missing("No conversation found with session ID abc"))
        self.assertTrue(self.tg.transcript_missing("Session abc not found"))


if __name__ == "__main__":
    unittest.main()
