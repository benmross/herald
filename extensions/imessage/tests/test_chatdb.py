"""The parts of reading a Mac's Messages database that can be wrong quietly.

None of this needs a Mac, which is the point: the timestamp arithmetic, the
attributedBody extraction and the awaiting-a-reply judgement are exactly the
three things that fail without raising anything, on a machine the author of a
change probably is not sitting at.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import sys
import unittest

EXT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXT / "lib"))

import chatdb  # noqa: E402


def _load_collector():
    """The collector imports herald.*, so load it by path rather than by name
    and let the test runner's PYTHONPATH supply the program."""
    spec = importlib.util.spec_from_file_location(
        "imessage_collector", EXT / "collectors" / "imessage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AppleTime(unittest.TestCase):
    #: 2023-06-01T00:00:00Z, in the units Core Data counts in.
    SECONDS = 707_270_400

    def test_nanoseconds_since_2001(self):
        """What every macOS since High Sierra actually stores."""
        self.assertEqual(chatdb.apple_time(self.SECONDS * 10**9),
                         dt.datetime(2023, 6, 1, tzinfo=dt.timezone.utc))

    def test_seconds_since_2001_still_parse(self):
        """Older databases, and the reason this looks at magnitude rather than
        at an OS version it cannot see."""
        self.assertEqual(chatdb.apple_time(self.SECONDS),
                         dt.datetime(2023, 6, 1, tzinfo=dt.timezone.utc))

    def test_the_two_forms_agree(self):
        self.assertEqual(chatdb.apple_time(self.SECONDS),
                         chatdb.apple_time(self.SECONDS * 10**9))

    def test_zero_and_none_are_no_timestamp(self):
        self.assertIsNone(chatdb.apple_time(0))
        self.assertIsNone(chatdb.apple_time(None))


class AttributedBody(unittest.TestCase):
    """Since Ventura the text column is often NULL and the message lives in a
    serialised NSAttributedString. A reader that only looks at `text` silently
    reports empty conversations."""

    def _blob(self, text: bytes) -> bytes:
        return (b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01\x40\x84\x84\x84"
                b"NSString\x01\x94\x84\x01\x2b"
                + bytes([len(text)]) + text
                + b"\x86\x84\x02iI\x00")

    def test_short_string(self):
        self.assertEqual(chatdb.attributed_body_text(self._blob(b"on my way")),
                         "on my way")

    def test_emoji_survive(self):
        raw = "see you at 8 👍".encode()
        self.assertEqual(chatdb.attributed_body_text(self._blob(raw)),
                         "see you at 8 👍")

    def test_a_long_string_uses_the_two_byte_length(self):
        text = ("x" * 400).encode()
        blob = (b"NSString\x01\x94\x84\x01\x2b\x81"
                + len(text).to_bytes(2, "little") + text)
        self.assertEqual(chatdb.attributed_body_text(blob), "x" * 400)

    def test_unrecoverable_is_empty_not_an_exception(self):
        """A message whose body cannot be read is still evidence about who
        spoke last, so it must not take the whole pass down."""
        self.assertEqual(chatdb.attributed_body_text(b"not a typedstream"), "")
        self.assertEqual(chatdb.attributed_body_text(None), "")


class AwaitingReply(unittest.TestCase):
    """'The last message wasn't from me' flags every robot and every group
    chat that carried on without you, and a list of forty unanswered
    conversations is one nobody reads -- which hides the one that mattered."""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_collector()
        cls.now = dt.datetime(2026, 9, 9, tzinfo=dt.timezone.utc)

    def _thread(self, **over):
        base = {"preview": "are you free tomorrow?", "handle_list": ["+15551234567"],
                "last_from_me": 0,
                "last_at": self.now - dt.timedelta(hours=3)}
        base.update(over)
        return base

    def test_a_question_from_a_person_is_waiting(self):
        needs, _ = self.mod._needs_reply(self._thread(), ["Sam"], self.now)
        self.assertTrue(needs)

    def test_your_own_last_word_is_not_waiting(self):
        needs, _ = self.mod._needs_reply(self._thread(last_from_me=1), ["Sam"], self.now)
        self.assertFalse(needs)

    def test_a_short_code_is_not_a_person(self):
        needs, _ = self.mod._needs_reply(
            self._thread(handle_list=["262966"]), ["Sam"], self.now)
        self.assertFalse(needs)

    def test_a_verification_code_is_never_waiting(self):
        needs, _ = self.mod._needs_reply(
            self._thread(preview="Your verification code is 448211. Do not share it?"),
            ["Sam"], self.now)
        self.assertFalse(needs)

    def test_a_big_group_is_a_room_not_a_question(self):
        needs, _ = self.mod._needs_reply(
            self._thread(handle_list=[f"+155500000{i}" for i in range(9)]),
            ["Sam"], self.now)
        self.assertFalse(needs)

    def test_being_named_counts_even_without_a_question(self):
        needs, _ = self.mod._needs_reply(
            self._thread(preview="Sam we're outside"), ["Sam"], self.now)
        self.assertTrue(needs)

    def test_a_short_remark_with_no_question_is_not_a_debt(self):
        needs, _ = self.mod._needs_reply(self._thread(preview="haha"), ["Sam"], self.now)
        self.assertFalse(needs)

    def test_something_left_a_month_ago_has_stopped_being_waiting(self):
        needs, _ = self.mod._needs_reply(
            self._thread(last_at=self.now - dt.timedelta(days=40)), ["Sam"], self.now)
        self.assertFalse(needs)


if __name__ == "__main__":
    unittest.main()
