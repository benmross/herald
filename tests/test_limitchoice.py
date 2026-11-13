"""Limit choices require fresh capacity and cannot revive stopped or newer work."""
import json
import time
import types
import unittest
from unittest import mock

from herald import limitchoice
import test_reply_resume as reply

KEY, CHAT, TOPIC = reply.KEY, reply.CHAT, reply.TOPIC


class Capacity(unittest.TestCase):
    def snapshot(self, used=20, reset=None):
        return {"sampled_at": time.time(), "windows": [
            {"label": "5-hour", "used_percent": used, "resets_at": reset}]}

    def test_switch_requires_positive_fresh_reading_for_every_window(self):
        good = self.snapshot(reset=time.time() + 3600)
        bad = [self.snapshot(100), self.snapshot(-1), self.snapshot(float("nan")),
               self.snapshot(10), self.snapshot(reset=time.time() - 1),
               {**good, "sampled_at": time.time() - 3600}, {**good, "windows": []},
               {**good, "windows": good["windows"] + self.snapshot(100)["windows"]}]
        with mock.patch.object(limitchoice.config, "enabled_engines", return_value=["codex"]):
            for snapshot, expected in [(good, True), (self.snapshot(0), True),
                                       *((s, False) for s in bad)]:
                with self.subTest(snapshot=snapshot), \
                        mock.patch.object(limitchoice.usage, "refresh", return_value=snapshot):
                    result = limitchoice.capacity(None, "codex")
                    self.assertEqual(result is not None, expected)
            with mock.patch.object(limitchoice.usage, "refresh", side_effect=RuntimeError("offline")):
                self.assertIsNone(limitchoice.capacity(None, "codex"))
                self.assertIsNone(limitchoice.capacity(None, "claude"))

    def test_card_shows_context_reset_model_effort_and_only_available_choices(self):
        ts = {"engine": "claude", "session_id": "stopped", "last_ts": 10,
              "work": {"request": "fix the wallpaper exporter"}}
        with mock.patch.object(limitchoice.config, "enabled_engines", return_value=["claude", "codex"]), \
                mock.patch.object(limitchoice.limitresume, "reset_at", return_value=time.time() + 3600), \
                mock.patch.object(limitchoice.config, "get", side_effect=lambda k, d=None:
                                  {"engines.codex.model": "gpt-6.1-sol", "engines.codex.effort": "high"}.get(k, d)):
            for snapshot, count in ((self.snapshot(), 3), (None, 2)):
                with mock.patch.object(limitchoice, "capacity", return_value=snapshot):
                    record, text, buttons = limitchoice.make(None, ts, "Export tests passed")
                self.assertEqual(len(buttons), count)
                self.assertIn("wallpaper exporter", text)
                self.assertIn("Export tests passed", text)
                self.assertIn("resets", text)
                self.assertIn("If you don't choose, I'll continue on Claude in this session", text)
                if snapshot:
                    self.assertIn("gpt-6.1-sol", text)
                    self.assertIn("high", text)


class Choices(unittest.TestCase):
    setUpClass = classmethod(reply.ReplyResume.setUpClass.__func__)
    setUp = reply.ReplyResume.setUp
    say = reply.ReplyResume.say

    def limited(self, engine="claude"):
        failed = types.SimpleNamespace(ok=False, cancelled=False, rate_limited=True, text="",
                                       session_id="stopped", cost_usd=None, error="usage limit")
        with mock.patch.object(self.tg.think, "think", return_value=failed), \
                mock.patch.object(self.tg.think, "resolve_engine", return_value=engine), \
                mock.patch.object(self.tg.config, "enabled_engines", return_value=["claude", "codex"]), \
                mock.patch.object(self.tg.limitchoice, "capacity", return_value={"windows": [
                    {"label": "5-hour", "used_percent": 20}]}), \
                mock.patch.object(self.tg.limitresume, "reset_at", return_value=time.time() + 3600):
            self.say("finish the exporter")
        return self.state["threads"][KEY]

    def tap(self, action, user=42, token=None, mid=None):
        record = self.state["threads"][KEY][limitchoice.KEY]
        cq = {"id": "tap", "from": {"id": user},
              "data": f"limit:{token or record['token']}:{action}",
              "message": {"message_id": mid or record["message_id"], "text": "Limit card",
                          "chat": {"id": CHAT}, "message_thread_id": TOPIC}}
        with mock.patch.object(self.tg, "_checkpoint"):
            self.tg._handle_limit_callback({"callback_query": cq}, self.state)
        return cq

    def test_switch_continues_saved_work_on_other_default_in_both_directions(self):
        for engine, other in (("claude", "codex"), ("codex", "claude")):
            with self.subTest(engine=engine):
                self.state["threads"].clear()
                ts = self.limited(engine)
                ts.update(model="custom", effort="max")
                ts[limitchoice.KEY].update(model="custom", effort="max")
                with mock.patch.object(limitchoice, "capacity", return_value={"ok": True}), \
                        mock.patch.object(self.tg.think, "resolve_engine", side_effect=lambda e: e), \
                        mock.patch.object(self.tg.config, "get", side_effect=lambda k, d=None:
                                          "target-default" if k == f"engines.{other}.model" else d):
                    self.tap("switch")
                self.assertEqual(self.calls[-1]["engine"], other)
                self.assertEqual(self.calls[-1]["model"], "target-default")
                self.assertIsNone(self.calls[-1]["effort"])
                self.assertIsNone(self.calls[-1]["resume"])
                self.assertIn("Earlier approvals do not carry over", self.prompts[-1])
                self.assertEqual(ts["work"]["request"], "finish the exporter")
                self.assertNotIn(limitchoice.KEY, ts)
                self.assertEqual(ts["previous_session"], "stopped")

    def test_wait_survives_restart_and_resumes_same_engine_once(self):
        self.limited()
        self.state = json.loads(json.dumps(self.state))
        ts = self.state["threads"][KEY]
        with mock.patch.object(self.tg.limitresume, "when", return_value=2000):
            cq = self.tap("wait")
        record = ts[self.tg.limitresume.KEY]
        self.assertEqual(record["at"], 2000)
        self.assertEqual(self.calls, [])
        with mock.patch.object(self.tg, "_checkpoint"), \
                mock.patch.object(self.tg.think, "resolve_engine", side_effect=lambda e: e):
            self.tg._run_limit_resume(self.state, KEY, record)
            self.tg._run_limit_resume(self.state, KEY, record)
            self.tg._handle_limit_callback({"callback_query": cq}, self.state)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[-1]["resume"], "stopped")
        self.assertEqual(self.calls[-1]["engine"], "claude")

    def test_an_unanswered_card_continues_the_same_session_after_reset(self):
        ts = self.limited()
        self.state = json.loads(json.dumps(self.state))     # survives a restart too
        ts = self.state["threads"][KEY]
        choice = ts[limitchoice.KEY]
        self.assertIn("If you don't choose", choice["text"])
        with mock.patch.object(self.tg, "_checkpoint"):
            self.tg._default_limit_choice(self.state, KEY, choice)
            self.tg._default_limit_choice(self.state, KEY, choice)   # a second tick is a no-op
        self.assertNotIn(limitchoice.KEY, ts)
        record = ts[self.tg.limitresume.KEY]
        self.assertEqual(record["engine"], "claude")
        with mock.patch.object(self.tg, "_checkpoint"), \
                mock.patch.object(self.tg.think, "resolve_engine", side_effect=lambda e: e):
            self.tg._run_limit_resume(self.state, KEY, record)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[-1]["resume"], "stopped")
        self.assertEqual(self.calls[-1]["engine"], "claude")

    def test_an_answered_card_is_not_defaulted(self):
        ts = self.limited()
        choice = ts[limitchoice.KEY]
        self.tap("stop")
        with mock.patch.object(self.tg, "_checkpoint"):
            self.tg._default_limit_choice(self.state, KEY, choice)
        self.assertNotIn(self.tg.limitresume.KEY, ts)
        self.assertEqual(self.calls, [])

    def test_stop_and_new_messages_invalidate_old_cards(self):
        ts = self.limited()
        cq = self.tap("stop")
        self.assertNotIn(self.tg.limitresume.KEY, ts)
        with mock.patch.object(self.tg, "handle") as run:
            self.tg._handle_limit_callback({"callback_query": cq}, self.state)
        run.assert_not_called()
        self.limited()
        self.say("something else")
        self.assertNotIn(limitchoice.KEY, ts)

    def test_foreign_or_mismatched_taps_cannot_continue(self):
        ts = self.limited()
        with mock.patch.object(self.tg, "handle") as run:
            self.tap("switch", user=99)
            self.tap("switch", token="0" * 16)
            self.tap("switch", mid=999999)
        run.assert_not_called()
        self.assertIn(limitchoice.KEY, ts)

    def test_stop_while_quota_is_being_read_wins_over_switch(self):
        ts = self.limited()

        def stopped(*args):
            self.tg.limitresume.cancel(ts)
            ts['last_ts'] = time.time()
            return {"ok": True}

        with mock.patch.object(limitchoice, "capacity", side_effect=stopped), \
                mock.patch.object(self.tg, "handle") as run:
            self.tap("switch")
        run.assert_not_called()
        self.assertEqual(ts["engine"], "claude")

    def test_ordinary_failure_does_not_offer_quota_buttons(self):
        failed = types.SimpleNamespace(ok=False, cancelled=False, rate_limited=False, text="",
                                       session_id="failed", cost_usd=None, error="network failure")
        with mock.patch.object(self.tg.think, "think", return_value=failed), \
                mock.patch.object(self.tg, "_offer_limit_choice") as offer:
            self.say("continue the exporter")
        offer.assert_not_called()

    def test_model_change_cancels_wait(self):
        ts = self.limited()
        with mock.patch.object(self.tg.limitresume, "when", return_value=2000):
            self.tap("wait")
        self.tg._select_model(ts, "codex", "chosen-model")
        self.assertNotIn(self.tg.limitresume.KEY, ts)

    def test_switch_rechecks_capacity_and_remains_paused_if_it_is_exhausted(self):
        ts = self.limited()
        with mock.patch.object(limitchoice, "capacity", return_value=None), \
                mock.patch.object(self.tg.limitresume, "reset_at", return_value=None), \
                mock.patch.object(self.tg, "handle") as run:
            self.tap("switch")
        run.assert_not_called()
        self.assertIsNone(ts[limitchoice.KEY]["other"])
        self.assertNotIn(self.tg.limitresume.KEY, ts)

    def test_stop_cancels_worker_that_was_already_queued(self):
        ts = self.limited()
        with mock.patch.object(self.tg.limitresume, "when", return_value=2000):
            self.tap("wait")
        record = ts[self.tg.limitresume.KEY]
        self.tg.limitresume.cancel(ts)
        with mock.patch.object(self.tg, "handle") as run, mock.patch.object(self.tg, "_checkpoint"):
            self.tg._run_limit_resume(self.state, KEY, record)
        run.assert_not_called()
