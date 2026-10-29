"""Topic-bound questions, asynchronous delivery and explicit consent taps."""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from herald import approvals, config, db, notify, private, questions


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(module)
    return module


class Questions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = load("questions_bridge_test", ROOT / "bin/herald-telegram")
        cls.cli = load("questions_cli_test", ROOT / "bin/herald")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.patch(config, "DB_PATH", pathlib.Path(tmp.name) / "facts.db")
        self.patch(config, "secret", mock.Mock(return_value=42))
        self.patch(notify, "_telegram_chat_id", mock.Mock(return_value=-100))
        self.patch(notify, "_updates_thread_id", mock.Mock(return_value=5))
        self.api = self.patch(notify, "_telegram_api", mock.Mock(
            return_value={"ok": True, "result": {"message_id": 77}}))
        env = mock.patch.dict("os.environ", {"HERALD_TELEGRAM_TOPIC": "-100:9"})
        env.start()
        self.addCleanup(env.stop)
        self.message = {"message_id": 77, "chat": {"id": -100}, "message_thread_id": 9}

    def patch(self, obj, attr, value):
        patcher = mock.patch.object(obj, attr, value)
        patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def ask(self, **kwargs):
        return questions.request("Which format?", options=["CSV", "JSON"], **kwargs)

    def tap(self, row, choice=0, **kwargs):
        return questions.answer(row["id"], "", sender=kwargs.get("sender", 42),
                                message=kwargs.get("message", self.message), choice=choice)

    def test_topic_routing_and_literal_question(self):
        self.ask()
        sent = self.api.call_args.kwargs
        self.assertEqual(sent["message_thread_id"], 9)
        self.assertEqual(sent["chat_id"], -100)
        self.assertNotIn("parse_mode", sent)
        self.assertIn("Which format?", sent["text"])
        self.assertEqual(len(sent["reply_markup"]["inline_keyboard"]), 2)

    def test_general_topic_is_distinct_from_updates(self):
        with mock.patch.dict("os.environ", {"HERALD_TELEGRAM_TOPIC": "-100:main"}):
            self.ask()
        self.assertNotIn("message_thread_id", self.api.call_args.kwargs)

    def test_foreign_chat_topic_falls_back_to_updates(self):
        with mock.patch.dict("os.environ", {"HERALD_TELEGRAM_TOPIC": "-999:9"}):
            self.ask()
        self.assertEqual(self.api.call_args.kwargs["message_thread_id"], 5)

    def test_answer_persists_and_cannot_be_changed(self):
        row = self.ask()
        self.assertEqual(self.tap(row, 1)["answer"], "JSON")
        self.assertIsNone(self.tap(row, 0))
        self.assertEqual(questions.result(row["id"])["answer"], "JSON")
        self.assertEqual(db.query("SELECT count(*) n FROM approvals")[0]["n"], 0)

    def test_owner_message_chat_and_topic_are_bound(self):
        row = self.ask()
        for changes in ({"chat": {"id": -999}}, {"message_thread_id": 10}, {"message_id": 78}):
            self.assertIsNone(self.tap(row, message=self.message | changes))
        self.assertIsNone(self.tap(row, sender=43))
        self.assertIsNone(self.tap(row, choice=99))
        self.assertEqual(questions.get(row["id"])["state"], "pending")

    def test_text_reply_answers_only_the_question_replied_to(self):
        row = questions.request("Name the page")
        msg = {"chat": {"id": -100}, "message_thread_id": 9, "from": {"id": 42},
               "reply_to_message": {"message_id": 77}, "text": "Overview"}
        self.assertIsNone(questions.reply(msg | {"reply_to_message": {"message_id": 78}}))
        self.assertEqual(questions.reply(msg)["answer"], "Overview")
        self.assertEqual(questions.get(row["id"])["answer"], "Overview")

    def test_approval_requires_a_tap_and_creates_one_consent_receipt(self):
        row = questions.request("Post this exact reply to the specified issue.", approval=True)
        self.assertIsNone(questions.answer(row["id"], "yes", sender=42, message=self.message))
        answered = self.tap(row)
        self.assertTrue(answered["approved"])
        self.assertEqual(approvals.get(row["approval_id"])["state"], "done")
        self.assertTrue(questions.result(row["id"])["approved"])
        self.assertIsNone(self.tap(row))
        actions = db.query("SELECT * FROM actions")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["kind"], "consent")
        self.assertEqual(actions[0]["approval_id"], row["approval_id"])
        self.assertIn("consent granted", actions[0]["summary"])

    def test_decline_grants_nothing(self):
        row = questions.request("Proceed?", approval=True)
        self.assertFalse(self.tap(row, 1)["approved"])
        self.assertEqual(approvals.get(row["approval_id"])["state"], "denied")
        self.assertFalse(db.query("SELECT * FROM actions"))

    def test_expired_question_and_approval_cannot_be_answered(self):
        row = questions.request("Proceed?", approval=True)
        with db.session() as con:
            con.execute("UPDATE questions SET expires_at=0 WHERE id=?", (row["id"],))
        self.assertIsNone(self.tap(row))
        self.assertEqual(questions.get(row["id"])["state"], "expired")
        self.assertEqual(approvals.get(row["approval_id"])["state"], "expired")

    def test_failed_delivery_expires_both_records(self):
        self.api.return_value = None
        with self.assertRaises(RuntimeError):
            questions.request("Proceed?", approval=True)
        self.assertEqual(db.query("SELECT state FROM questions")[0]["state"], "expired")
        self.assertEqual(db.query("SELECT state FROM approvals")[0]["state"], "expired")

    def test_oversized_and_invalid_questions_are_not_sent(self):
        for kwargs in ({"question": "😀" * 2500}, {"question": ""},
                       {"question": "Q?", "options": ["same", "same"]},
                       {"question": "Q?", "approval": True, "options": ["Yes"]}):
            with self.assertRaises(ValueError):
                questions.request(**kwargs)
        self.api.assert_not_called()

    def test_private_sessions_cannot_ask(self):
        with mock.patch.dict("os.environ", {private.ENV: "1"}):
            with self.assertRaises(private.Refused):
                self.ask()
        self.api.assert_not_called()

    def test_blocking_wait_returns_answer_and_disables_async_resume(self):
        row = self.ask(wake=False)
        with mock.patch.object(questions.time, "sleep", side_effect=lambda _: self.tap(row, 1)):
            answered = questions.wait(row["id"])
        self.assertEqual(answered["answer"], "JSON")
        self.assertFalse(answered["wake"])

    def test_async_answer_steers_live_topic(self):
        row = self.tap(self.ask())
        with mock.patch.object(self.bridge, "api"), mock.patch.object(self.bridge, "send"), \
                mock.patch.object(self.bridge, "_try_steer", return_value=True) as steer, \
                mock.patch.object(self.bridge, "handle") as handle:
            self.bridge._question_answer(row, {"update_id": 1}, {})
        update, key, _ = steer.call_args.args
        self.assertEqual(key, "-100:9")
        self.assertIn("My answer: CSV", update["message"]["text"])
        handle.assert_not_called()

    def test_async_answer_resumes_idle_topic(self):
        row = self.tap(self.ask())
        with mock.patch.object(self.bridge, "api"), mock.patch.object(self.bridge, "send"), \
                mock.patch.object(self.bridge, "_try_steer", return_value=False), \
                mock.patch.object(self.bridge, "handle") as handle:
            self.bridge._question_answer(row, {"update_id": 1}, {})
        handle.assert_called_once()

    def test_blocking_answer_does_not_start_a_second_turn(self):
        row = self.tap(self.ask(wake=False))
        with mock.patch.object(self.bridge, "api"), mock.patch.object(self.bridge, "send"), \
                mock.patch.object(self.bridge, "_try_steer") as steer:
            self.bridge._question_answer(row, {"update_id": 1}, {})
        steer.assert_not_called()

    def test_callback_owner_check_precedes_question_lookup(self):
        update = {"callback_query": {"id": "tap", "from": {"id": 43},
                  "data": "question:1:0", "message": self.message}}
        with mock.patch.object(self.bridge, "api") as api, \
                mock.patch.object(questions, "answer") as answer:
            self.bridge._handle_question_callback(update, {})
        answer.assert_not_called()
        self.assertEqual(api.call_args.kwargs["text"], "Not your Herald.")

    def test_private_topic_text_is_not_stored_as_an_answer(self):
        update = {"message": {"chat": {"id": -100}, "message_thread_id": 9,
                              "reply_to_message": {"message_id": 77}, "text": "private"}}
        with mock.patch.dict(self.bridge._private, {"-100:9": {}}), \
                mock.patch.object(questions, "reply") as reply:
            self.assertFalse(self.bridge._question_text(update, {}))
        reply.assert_not_called()

    def test_dispatch_records_reply_before_regular_message_steering(self):
        self.ask()
        update = {"update_id": 99, "message": {"chat": {"id": -100}, "message_thread_id": 9,
                  "from": {"id": 42}, "reply_to_message": {"message_id": 77}, "text": "JSON"}}
        with mock.patch.object(self.bridge, "_question_answer") as answered, \
                mock.patch.object(self.bridge, "_checkpoint"), \
                mock.patch.object(self.bridge, "_maybe_restart"), \
                mock.patch.object(self.bridge, "_try_steer") as steer:
            self.bridge._dispatch(update, {})
        answered.assert_called_once()
        self.assertEqual(answered.call_args.args[0]["answer"], "JSON")
        steer.assert_not_called()

    def test_cli_exposes_both_modes_for_questions_and_approvals(self):
        for approval in (False, True):
            for wait in (False, True):
                argv = ["herald", "ask", "Proceed?", "--wait" if wait else "--no-wait"]
                if approval:
                    argv.append("--approval")
                with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()), \
                        mock.patch.object(questions, "request", return_value={
                            "id": 1, "approval_id": None, "state": "pending"}) as request, \
                        mock.patch.object(questions, "wait", return_value={
                            "id": 1, "approval_id": None, "state": "answered"}) as waiting:
                    self.assertEqual(self.cli.main(), 0)
                self.assertEqual(request.call_args.kwargs["wake"], not wait)
                self.assertEqual(request.call_args.kwargs["approval"], approval)
                self.assertEqual(waiting.call_count, int(wait))


if __name__ == "__main__":
    unittest.main()
