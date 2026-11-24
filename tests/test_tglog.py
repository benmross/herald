"""The record of what went to Telegram and what came back (23 Nov 2026).

What must hold: a sent message is recorded with its text and the id Telegram
gave it; a failure is recorded as one; calls that show the user nothing are
not recorded; a private topic's rows carry no text; a failure to write the
record never reaches the caller; old rows are pruned.
"""
from __future__ import annotations

import contextlib
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import db, tgapi, tglog  # noqa: E402


class Log(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = pathlib.Path(self.tmp.name) / "test.db"
        with contextlib.closing(sqlite3.connect(path)) as con:
            con.executescript(db.SCHEMA)

        @contextlib.contextmanager
        def session():
            con = sqlite3.connect(path, timeout=10)
            con.row_factory = sqlite3.Row
            try:
                with con:
                    yield con
            finally:
                con.close()
        patch = mock.patch.object(db, "session", session)
        patch.start()
        self.addCleanup(patch.stop)
        tglog.private_topic = None
        self.addCleanup(setattr, tglog, "private_topic", None)

    def test_a_sent_message_keeps_its_text_and_the_id_telegram_gave_it(self):
        tglog.sent("sendMessage", {"chat_id": -5, "message_thread_id": 9, "text": "Hello *there*", "parse_mode": "HTML"},
                   {"ok": True, "result": {"message_id": 41, "message_thread_id": 9}})
        (r,) = tglog.rows()
        self.assertEqual((r["direction"], r["method"], r["chat_id"], r["thread_id"], r["message_id"], r["ok"], r["text"]),
                         ("out", "sendMessage", -5, 9, 41, 1, "Hello *there*"))

    def test_an_edit_is_found_by_its_message(self):
        tglog.sent("sendMessage", {"chat_id": 1, "text": "one"}, {"ok": True, "result": {"message_id": 7}})
        tglog.sent("editMessageText", {"chat_id": 1, "message_id": 7, "text": "one, two"}, {"ok": True, "result": {"message_id": 7}})
        tglog.sent("sendMessage", {"chat_id": 1, "text": "other"}, {"ok": True, "result": {"message_id": 8}})
        self.assertEqual([r["text"] for r in tglog.rows(message_id=7)], ["one", "one, two"])

    def test_a_failure_and_no_response_are_recorded_as_failures(self):
        tglog.sent("sendMessage", {"chat_id": 1, "text": "x"}, {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"})
        tglog.sent("sendMessage", {"chat_id": 1, "text": "y"}, None)
        a, b = tglog.rows()
        self.assertEqual((a["ok"], b["ok"]), (0, 0))
        self.assertIn("can't parse entities", a["error"])
        self.assertEqual(b["error"], "no response")

    def test_polling_and_typing_are_not_recorded(self):
        tglog.sent("getUpdates", {"offset": 1}, {"ok": True, "result": []})
        tglog.sent("sendChatAction", {"chat_id": 1, "action": "typing"}, {"ok": True})
        self.assertEqual(tglog.rows(), [])

    def test_a_private_topic_is_recorded_without_its_text(self):
        tglog.private_topic = lambda chat, thread: (chat, thread) == (1, 3)
        tglog.sent("sendMessage", {"chat_id": 1, "message_thread_id": 3, "text": "secret"}, {"ok": True, "result": {"message_id": 5}})
        tglog.received({"message": {"message_id": 6, "chat": {"id": 1}, "message_thread_id": 3, "text": "also secret", "from": {"id": 2}}})
        tglog.sent("sendMessage", {"chat_id": 1, "message_thread_id": 4, "text": "open"}, {"ok": True, "result": {"message_id": 7}})
        hidden_out, hidden_in, shown = tglog.rows()
        self.assertEqual((hidden_out["private"], hidden_out["text"], hidden_out["message_id"]), (1, None, 5))
        self.assertEqual((hidden_in["private"], hidden_in["text"], hidden_in["extra"]), (1, None, None))
        self.assertEqual((shown["private"], shown["text"]), (0, "open"))
        self.assertEqual(tglog.rows(grep="secret"), [])

    def test_a_private_process_records_no_text(self):
        with mock.patch.object(tglog.private, "active", lambda: True):
            tglog.sent("sendMessage", {"chat_id": 1, "text": "secret"}, {"ok": True, "result": {"message_id": 5}})
        self.assertEqual([r["text"] for r in tglog.rows()], [None])

    def test_received_messages_taps_and_files(self):
        tglog.received({"message": {"message_id": 3, "chat": {"id": 1}, "text": "Why?", "from": {"id": 2}}})
        tglog.received({"callback_query": {"data": "approve:9", "from": {"id": 2}, "message": {"message_id": 4, "chat": {"id": 1}}}})
        tglog.received({"message": {"message_id": 5, "chat": {"id": 1}, "caption": "see", "document": {"file_name": "a.pdf"}, "from": {"id": 2}}})
        a, b, c = tglog.rows(direction="in")
        self.assertEqual((a["method"], a["text"], b["method"], b["text"], c["text"]), ("message", "Why?", "callback_query", "approve:9", "see"))
        self.assertIn("a.pdf", c["extra"])

    def test_buttons_and_file_names_are_kept(self):
        tglog.sent("sendMessage", {"chat_id": 1, "text": "Publish?", "reply_markup": {"inline_keyboard": [[{"text": "Approve"}, {"text": "Decline"}]]}},
                   {"ok": True, "result": {"message_id": 5}})
        tglog.sent("sendDocument", {"chat_id": 1, "caption": "the plan"}, {"ok": True, "result": {"message_id": 6}}, filename="plan.md")
        a, b = tglog.rows()
        self.assertIn("Approve", a["extra"])
        self.assertEqual((b["text"], "plan.md" in b["extra"]), ("the plan", True))

    def test_the_transport_records_what_it_sent(self):
        with mock.patch.object(tgapi._client, "call", lambda method, **kw: {"ok": True, "result": {"message_id": 12}}):
            tgapi.call("sendMessage", chat_id=1, text="through the transport")
        self.assertEqual(tglog.rows()[0]["text"], "through the transport")

    def test_a_record_that_cannot_be_written_does_not_fail_the_send(self):
        with mock.patch.object(db, "session", side_effect=RuntimeError("locked")):
            tglog.sent("sendMessage", {"chat_id": 1, "text": "x"}, {"ok": True, "result": {"message_id": 1}})

    def test_old_rows_are_pruned_and_zero_keeps_everything(self):
        tglog.sent("sendMessage", {"chat_id": 1, "text": "old"}, {"ok": True, "result": {"message_id": 1}})
        with db.session() as con:
            con.execute("UPDATE telegram_log SET ts='2020-01-01T00:00:00.000+00:00'")
        tglog.sent("sendMessage", {"chat_id": 1, "text": "new"}, {"ok": True, "result": {"message_id": 2}})
        self.assertEqual(tglog.prune(0), 0)
        self.assertEqual(tglog.prune(30), 1)
        self.assertEqual([r["text"] for r in tglog.rows()], ["new"])

    def test_since_takes_a_span_or_a_time(self):
        self.assertLess(tglog.since("2h"), tglog.since("30m"))
        self.assertTrue(tglog.since("2026-11-23T22:26").startswith("2026-11-23T22:26") or "2026-11-2" in tglog.since("2026-11-23T22:26"))


if __name__ == "__main__":
    unittest.main()
