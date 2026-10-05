"""Nothing the model says while it works may be dropped on the way to a chat.

The live transcript exists because watching a turn in a terminal is pleasant
and watching a list of file paths is not. Two versions of this code lost text
without saying so -- a 400-character truncation of each narration block, and a
24-line cap on the transcript -- and neither would have shown up as an error.
These tests are the guard.
"""

from __future__ import annotations

import json
import pathlib
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import think  # noqa: E402


def _events(*messages) -> list:
    """Feed assistant messages through the real stream parser."""
    seen = []
    state: dict = {}
    for content in messages:
        line = json.dumps({"type": "assistant", "message": {"content": content}})
        think._handle_stream_line(line, state, seen.append)
    return seen


class NothingIsDropped(unittest.TestCase):
    def test_narration_and_tools_arrive_in_order(self):
        seen = _events(
            [{"type": "text", "text": "Reading the collector first."}],
            [{"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}],
            [{"type": "text", "text": "Found it."}],
        )
        self.assertEqual([e.kind for e in seen], ["text", "tool", "text"])
        self.assertEqual(seen[0].text, "Reading the collector first.")
        self.assertIn("a.py", seen[1].text)

    def test_several_blocks_in_one_message_all_arrive(self):
        """An assistant message can carry narration and a call together."""
        seen = _events([
            {"type": "text", "text": "First."},
            {"type": "text", "text": "Second."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
        ])
        self.assertEqual([e.kind for e in seen], ["text", "text", "tool"])

    def test_a_long_thought_is_not_truncated(self):
        """It used to be cut at 400 characters, which is mid-sentence for
        anything worth reading."""
        long = "word " * 800
        seen = _events([{"type": "text", "text": long}])
        self.assertEqual(seen[0].text, long.strip())
        self.assertGreater(len(seen[0].text), 3000)

    def test_line_breaks_survive(self):
        seen = _events([{"type": "text", "text": "one\n\ntwo\nthree"}])
        self.assertEqual(seen[0].text, "one\n\ntwo\nthree")

    def test_empty_text_is_not_an_event(self):
        self.assertEqual(_events([{"type": "text", "text": "   "}]), [])

    def test_thinking_blocks_carry_nothing_to_show(self):
        """`claude -p` emits a thinking block with an empty body and a
        signature -- the reasoning itself is not in the stream, so there is
        nothing here to display and this must not produce a blank line."""
        seen = _events([{"type": "thinking", "thinking": "", "signature": "abc"}])
        self.assertEqual(seen, [])


class TheTranscript(unittest.TestCase):
    """The renderer, with Telegram's API stubbed out."""

    def setUp(self):
        src = (ROOT / "bin" / "herald-telegram").read_text()
        self.mod = types.ModuleType("tg")
        self.mod.__dict__["__file__"] = str(ROOT / "bin" / "herald-telegram")
        exec(compile(src.split("def main()")[0], "herald-telegram", "exec"),
             self.mod.__dict__)
        self.sent = []
        self.mod.api = lambda method, **kw: (
            self.sent.append((method, kw)) or {"ok": True, "result": {"message_id": 7}})
        self.w = self.mod.Working(chat_id=1)

    def test_every_line_appears(self):
        for i in range(40):
            self.w.on_progress(think.Progress("text", f"thought number {i}"))
        body = self.w._render()
        for i in range(40):
            self.assertIn(f"thought number {i}", body)

    def test_an_overlong_transcript_trims_the_front_and_says_so(self):
        self.w.on_progress(think.Progress("text", "x" * 6000))
        body = self.w._render()
        self.assertIn("earlier steps trimmed to fit", body)
        self.assertLess(len(body), 4096)

    def test_the_newest_text_is_always_present(self):
        self.w.on_progress(think.Progress("text", "y" * 6000))
        self.w.on_progress(think.Progress("text", "the last thing it said"))
        self.assertIn("the last thing it said", self.w._render())

    def test_settle_trims_the_duplicated_final_answer(self):
        self.w.on_progress(think.Progress("text", "Looking at the collector."))
        self.w.on_progress(think.Progress("tool", "Read: a.py"))
        self.w.on_progress(think.Progress("text", "The cursor is never read back."))
        self.w.settle("The cursor is never read back, so every pass re-reads "
                      "the whole window.")
        self.assertNotIn("The cursor is never read back", self.w._sent_text)
        self.assertIn("Looking at the collector", self.w._sent_text)

    def test_settle_trims_the_answer_even_when_it_is_the_only_prose(self):
        """The reply is sent right below; repeating it wastes the screen."""
        self.w.on_progress(think.Progress("tool", "Read: a.py"))
        self.w.on_progress(think.Progress("text", "Both words are **alpha** and beta."))
        self.w.settle("Both words are alpha and beta.")
        self.assertNotIn("alpha and beta", self.w._sent_text)
        self.assertIn("Read: a.py", self.w._sent_text)

    def test_settle_trims_a_reply_split_across_blocks(self):
        self.w.on_progress(think.Progress("tool", "Read: a.py"))
        self.w.on_progress(think.Progress("text", "First paragraph of the answer."))
        self.w.on_progress(think.Progress("text", "Second paragraph of it."))
        self.w.settle("First paragraph of the answer.\n\nSecond paragraph of it.")
        self.assertNotIn("paragraph", self.w._sent_text)

    def test_a_settled_turn_with_only_prose_shows_just_the_header(self):
        self.w.on_progress(think.Progress("text", "The answer is four."))
        self.w._publish()
        self.w.settle("The answer is four.")
        self.assertNotIn("four", self.w._sent_text)
        self.assertNotIn("thinking", self.w._sent_text)
        self.assertIn("done", self.w._sent_text)

    def test_the_header_says_done_when_settled(self):
        self.w.on_progress(think.Progress("tool", "Read: a.py"))
        self.w.settle("done")
        self.assertIn("done", self.w._sent_text)

    def test_a_turn_with_no_events_sends_nothing(self):
        """A two-second answer should not get a progress message at all."""
        self.w._finish()
        self.assertEqual(self.sent, [])


class AReplyHeldForBackgroundWork(unittest.TestCase):
    """A turn that answers, waits on a background task, and answers again has
    given two replies. think returns only the last `result`, so the first has
    to leave by another door or it survives only as transcript."""

    def test_a_surface_that_takes_it_gets_it_once(self):
        seen = []
        state = {"payload": {"result": "First half."}}
        think._offer_interim(state, lambda e: seen.append(e) or True)
        self.assertEqual([(e.kind, e.text) for e in seen], [("interim", "First half.")])
        state["payload"] = {"result": "Second half."}
        think._fold_interim(state)
        self.assertEqual(state["payload"]["result"], "Second half.")

    def test_a_caller_that_ignores_it_still_gets_the_words(self):
        for callback in (None, lambda e: None, lambda e: 1 / 0):
            state = {"payload": {"result": "First half."}}
            think._offer_interim(state, callback)
            state["payload"] = {"result": "Second half."}
            think._fold_interim(state)
            self.assertEqual(state["payload"]["result"], "First half.\n\nSecond half.")

    def test_the_held_reply_returned_on_timeout_is_not_doubled(self):
        state = {"payload": {"result": "Only answer."}}
        think._offer_interim(state, None)
        think._fold_interim(state)
        self.assertEqual(state["payload"]["result"], "Only answer.")

    def test_an_empty_held_result_is_nothing(self):
        seen = []
        state = {"payload": {"result": "  "}}
        think._offer_interim(state, seen.append)
        self.assertEqual(seen, [])
        self.assertFalse(state.get("held_texts"))


class TheBridgeSendsAnInterimReply(TheTranscript):
    def test_it_goes_out_as_a_reply_and_leaves_the_transcript(self):
        w = self.w
        w.on_progress(think.Progress("tool", "Agent: check postings"))
        w.on_progress(think.Progress("text", "**Here** is the first half."))
        self.assertIs(w.on_progress(think.Progress("interim", "**Here** is the first half.")), True)
        sends = [kw["text"] for m, kw in self.sent if m == "sendMessage"]
        self.assertTrue(any("<b>Here</b> is the first half." in t for t in sends))
        closed = [kw["text"] for m, kw in self.sent
                  if "Agent: check postings" in kw.get("text", "")][-1]
        self.assertNotIn("first half", closed)
        self.assertEqual(w._lines, [])
        self.assertIsNone(w._msg_id)
        w.on_progress(think.Progress("tool", "Bash: later"))
        self.assertNotIn("check postings", w._render())

    def test_the_same_reply_is_not_sent_twice(self):
        self.w.on_progress(think.Progress("interim", "The answer, held."))
        self.assertTrue(self.w.already_sent("The answer, held.\n"))
        self.assertFalse(self.w.already_sent("A different final answer."))
        self.assertFalse(self.w.already_sent(""))

    def test_no_empty_block_is_posted_before_it(self):
        self.w.on_progress(think.Progress("interim", "Straight to the point."))
        self.assertEqual(len([m for m, _ in self.sent if m == "sendMessage"]), 1)


if __name__ == "__main__":
    unittest.main()
