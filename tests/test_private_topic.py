"""`/private` in a Telegram topic: the bridge's half of a private session.

The sandbox is tested in test_private.py. What is tested here is that the
bridge keeps a private session apart from the topic's own conversation, hands
`think` the scratch directory, files nothing, and deletes the scratch when the
session ends.
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

import test_reply_resume as base


class PrivateTopic(unittest.TestCase):
    setUp_ = base.ReplyResume.setUp
    say = base.ReplyResume.say

    @classmethod
    def setUpClass(cls):
        cls.tg = base._load()

    def setUp(self):
        self.setUp_()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patch in (
            mock.patch.object(self.tg.private_mod, "_ram_root",
                              return_value=pathlib.Path(tmp.name) / "root"),
            mock.patch.object(self.tg.private_mod, "unavailable", return_value=None),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.tg._private.clear)

    def rows(self) -> int:
        return self.con.execute("SELECT count(*) FROM telegram_messages").fetchone()[0]

    def test_a_private_turn_gets_the_scratch_and_is_filed_nowhere(self):
        self.say("an ordinary message")
        before, stored = self.rows(), dict(self.state["threads"][base.KEY])
        self.say("/private")
        self.say("a private message")
        call = self.calls[-1]
        self.assertTrue(pathlib.Path(call["private"]).is_dir())
        self.assertIsNone(call["resume"])
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.state["threads"][base.KEY], stored)
        self.assertNotIn(call["private"], str(self.state))

    def test_an_ordinary_turn_is_not_private(self):
        self.say("an ordinary message")
        self.assertIsNone(self.calls[-1]["private"])

    def test_the_second_private_message_continues_the_first(self):
        self.say("an ordinary message")          # sess-A
        self.say("/private")
        self.say("one")                          # sess-B, private
        self.say("two")
        self.assertEqual(self.calls[-1]["resume"], "sess-B")
        self.assertEqual(self.calls[-1]["private"], self.calls[-2]["private"])

    def test_off_deletes_it_and_the_topic_is_where_it_was(self):
        self.say("an ordinary message")          # sess-A
        self.say("/private")
        self.say("one")                          # sess-B, private
        scratch = pathlib.Path(self.calls[-1]["private"])
        self.say("/private off")
        self.assertFalse(scratch.exists())
        self.say("back to normal")
        self.assertEqual(self.calls[-1]["resume"], "sess-A")
        self.assertIsNone(self.calls[-1]["private"])

    def test_new_starts_another_private_session_and_deletes_the_last(self):
        self.say("an ordinary message")          # sess-A
        self.say("/private")
        self.say("one")                          # sess-B, private
        first = pathlib.Path(self.calls[-1]["private"])
        self.say("/new")
        self.assertFalse(first.exists())
        self.say("two")
        self.assertIsNone(self.calls[-1]["resume"])
        self.assertNotEqual(self.calls[-1]["private"], str(first))
        self.assertEqual(self.state["threads"][base.KEY]["session_id"], "sess-A")

    def test_a_reply_to_an_old_message_does_not_leave_private(self):
        first = self.say("an ordinary message")  # sess-A
        self.say("/new")
        self.say("another")                      # sess-B
        self.say("/private")
        self.say("replying", reply_to=first)
        self.assertIsNotNone(self.calls[-1]["private"])
        self.assertIsNone(self.calls[-1]["resume"])

    def test_where_there_is_no_sandbox_it_says_so_and_stays_ordinary(self):
        self.tg.private_mod.unavailable.return_value = "no bubblewrap here"
        self.say("/private")
        self.assertIn("no bubblewrap here", self.sent[-1])
        self.say("a message")
        self.assertIsNone(self.calls[-1]["private"])

    def test_the_log_does_not_quote_a_private_message(self):
        self.say("/private")
        with mock.patch("builtins.print") as out:
            self.say("the secret thing")
        self.assertNotIn("secret", " ".join(str(c) for c in out.call_args_list))


if __name__ == "__main__":
    unittest.main()
