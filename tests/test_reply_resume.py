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
import time
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
        self.prompts: list[str] = []
        self.sessions = iter(["sess-A", "sess-B", "sess-C"])

        def api(method, **kw):
            if method != "sendMessage":
                return {"ok": True, "result": True}
            self.next_id += 1
            self.sent.append(kw["text"])
            return {"ok": True, "result": {"message_id": self.next_id}}

        def think_(prompt, **kw):
            self.calls.append(kw)
            self.prompts.append(prompt)
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
                              side_effect=lambda ts, fresh: "card" if fresh else ts.get("orientation")),
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

    def test_new_abandons_the_work_and_a_renewal_continues_it(self):
        self.say("build the thing")
        ts = self.state["threads"][KEY]
        self.assertEqual(ts["work"]["request"], "build the thing")
        ts.update(context_tokens=900, context_window=1000)
        self.say("keep going")
        self.assertEqual(ts["session_id"], "sess-B")
        self.assertEqual(ts["work"]["request"], "build the thing")
        ts["handoff_from"] = "stale"
        self.say("/new")
        self.assertNotIn("handoff_from", ts)
        self.say("something else")
        self.assertEqual(ts["work"]["request"], "something else")

    def test_auto_finishes_the_turn_on_the_other_provider_at_a_limit(self):
        results = iter([
            types.SimpleNamespace(ok=False, cancelled=False, rate_limited=True, text="",
                                  session_id="sess-A", cost_usd=None, error="limit"),
            types.SimpleNamespace(ok=True, cancelled=False, text="done on codex",
                                  session_id="sess-X", cost_usd=None, error=None)])
        engines = []

        def limited(prompt, **kw):
            engines.append(kw["engine"])
            return next(results)

        with mock.patch.object(self.tg.think, "think", limited), \
                mock.patch.object(self.tg.routing, "enabled", return_value=True), \
                mock.patch.object(self.tg.routing, "before_turn", return_value=None), \
                mock.patch.object(self.tg.routing, "alternative", return_value="codex"), \
                mock.patch.object(self.tg.think, "resolve_engine", side_effect=lambda e: e or "claude"):
            self.say("finish this")
        ts = self.state["threads"][KEY]
        self.assertEqual(engines, ["claude", "codex"])
        self.assertEqual((ts["engine"], ts["session_id"]), ("codex", "sess-X"))
        self.assertTrue(any("hit its limit" in m for m in self.sent))
        self.assertTrue(any("done on codex" in m for m in self.sent))

    def test_new_cards_follow_static_preamble_and_precede_request(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                self.state["threads"].clear()
                with mock.patch.object(self.tg.config, "default_engine", return_value=provider):
                    self.say("new request")
                self.assertIsNone(self.calls[-1]["append_system_prompt"])
                prompt = self.prompts[-1]
                self.assertTrue(prompt.startswith(self.tg.PREAMBLE))
                self.assertLess(prompt.index("card"), prompt.index("new request"))
                self.assertIsNone(self.state["threads"][KEY]["orientation"])
                self.say("follow-up request")
                self.assertNotIn("card", self.prompts[-1])
                self.assertIsNone(self.calls[-1]["append_system_prompt"])

    def test_existing_system_cards_are_preserved_on_resume(self):
        self.state["threads"][KEY] = {"session_id": "legacy", "engine": "claude",
                                       "turns": 1, "last_ts": time.time(),
                                       "orientation": "old system card"}
        self.say("follow-up")
        self.assertEqual(self.calls[-1]["append_system_prompt"], "old system card")
        self.assertNotIn("old system card", self.prompts[-1])

    def test_missing_transcript_retry_moves_the_new_card_into_user_input(self):
        self.state["threads"][KEY] = {"session_id": "legacy", "engine": "claude",
                                       "turns": 1, "last_ts": time.time(),
                                       "orientation": "old system card"}
        failed = types.SimpleNamespace(ok=False, cancelled=False, text="", session_id="legacy",
                                       cost_usd=0.0, error="No conversation found")
        success = types.SimpleNamespace(ok=True, cancelled=False, text="recovered", session_id="fresh",
                                        cost_usd=0.0, error=None)
        with mock.patch.object(self.tg.think, "think", side_effect=[failed, success]) as run:
            self.say("recover this request")
        before, after = run.call_args_list
        self.assertEqual(before.kwargs["append_system_prompt"], "old system card")
        self.assertIsNone(after.kwargs["append_system_prompt"])
        self.assertIn("card", after.args[0])
        self.assertNotIn("old system card", after.args[0])
        self.assertTrue(after.args[0].endswith("recover this request"))
        self.assertEqual(self.pointer, "fresh")

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
