"""The update command reaches the chosen session with the updater workflow."""

import unittest
from unittest import mock

import test_reply_resume as reply


class UpdateCommand(unittest.TestCase):
    setUpClass = classmethod(reply.ReplyResume.setUpClass.__func__)
    setUp = reply.ReplyResume.setUp
    say = reply.ReplyResume.say

    def test_addressed_command_reaches_existing_session_with_update_instructions(self):
        self.say("hello")
        self.say("/UPDATE@my_bot")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[-1]["resume"], "sess-A")
        self.assertIn("/UPDATE@my_bot", self.prompts[-1])
        self.assertIn("herald update --check", self.prompts[-1])
        self.assertIn(self.tg.PROMPT_COMMANDS["/update"], self.prompts[-1])
        self.assertEqual(self.tg.unknown_command("/update"), "")

    def test_private_update_does_not_start_a_model_or_touch_scratch(self):
        self.tg._private[reply.KEY] = {"scratch": "/unused", "session_id": None}
        self.addCleanup(self.tg._private.clear)
        self.say("/update")
        self.assertEqual(self.calls, [])
        self.assertIn("/private off", self.sent[-1])

    def test_another_sender_cannot_start_an_update(self):
        with mock.patch.object(self.tg.config, "secret", return_value=99):
            self.say("/update")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.sent, [])

    def test_update_is_a_command_and_cannot_steer_an_active_task(self):
        update = {"message": {"text": "/update@my_bot", "chat": {"id": reply.CHAT},
                              "from": {"id": 42}, "message_thread_id": reply.TOPIC}}
        with mock.patch.object(self.tg.think, "steer") as steer:
            self.assertFalse(self.tg._try_steer(update, reply.KEY, self.state))
        steer.assert_not_called()
