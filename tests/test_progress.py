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
    def test_stop_hook_feedback_retracts_the_draft(self):
        seen: list = []
        state: dict = {}
        think._handle_stream_line(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Draft."}]}}), state, seen.append)
        think._handle_stream_line(json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "Stop hook feedback:\n[x]: rewrite"}]}}), state, seen.append)
        self.assertEqual([e.kind for e in seen], ["text", "retract"])

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


class _Bridge(unittest.TestCase):
    """The bridge's renderer, with Telegram's API stubbed out."""

    NARRATION = "transcript"

    def setUp(self):
        src = (ROOT / "bin" / "herald-telegram").read_text()
        self.mod = types.ModuleType("tg")
        self.mod.__dict__["__file__"] = str(ROOT / "bin" / "herald-telegram")
        exec(compile(src.split("def main()")[0], "herald-telegram", "exec"),
             self.mod.__dict__)
        self.sent = []
        def api(method, **kw):
            self.sent.append((method, kw))
            mid = (sum(m == "sendMessage" for m, _ in self.sent)
                   if method == "sendMessage" else kw.get("message_id", 7))
            return {"ok": True, "result": {"message_id": mid}}
        self.mod.api = api
        self.w = self.mod.Working(chat_id=1, narration=self.NARRATION)


class TheTranscript(_Bridge):
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
        self.assertIsNotNone(w._msg_id)
        w.on_progress(think.Progress("tool", "Bash: later"))
        self.assertNotIn("check postings", w._render())
        w._publish()
        self.assertEqual(len([m for m, _ in self.sent if m == "sendMessage"]), 2)
        self.assertIn("first half", self.sent[-1][1]["text"])
        self.assertIn("Bash: later", self.sent[-1][1]["text"])

    def test_the_same_reply_is_not_sent_twice(self):
        self.w.on_progress(think.Progress("interim", "The answer, held."))
        self.assertTrue(self.w.already_sent("The answer, held.\n"))
        self.assertFalse(self.w.already_sent("A different final answer."))
        self.assertFalse(self.w.already_sent(""))

    def test_no_empty_block_is_posted_before_it(self):
        self.w.on_progress(think.Progress("interim", "Straight to the point."))
        self.assertEqual(len([m for m, _ in self.sent if m == "sendMessage"]), 1)


class NarrationAsMessages(_Bridge):
    """What the model says between tool calls is sent as messages of its own."""

    NARRATION = "messages"

    def texts(self):
        return [kw["text"] for m, kw in self.sent if m == "sendMessage"]

    def test_text_followed_by_a_tool_call_is_sent_as_a_message(self):
        w = self.w
        w.on_progress(think.Progress("text", "Checking the collector first."))
        self.assertEqual(self.texts(), [])                       # held: it might be the answer
        w.on_progress(think.Progress("tool", "Read: a.py"))
        self.assertTrue(any("Checking the collector first." in t for t in self.texts()))
        w._publish()
        self.assertEqual(len(self.texts()), 1)
        self.assertEqual(w._render().count("Checking the collector"), 1)
        self.assertEqual(self.sent[-1][0], "editMessageText")
        self.assertEqual(self.sent[-1][1]["message_id"], w.message_ids[-1])
        self.assertIn("Read: a.py", w._render())

    def test_each_message_carries_its_following_tool_updates(self):
        w = self.w
        w.on_progress(think.Progress("text", "One."))
        w.on_progress(think.Progress("tool", "Bash: a"))
        w.on_progress(think.Progress("text", "Two."))
        w.on_progress(think.Progress("tool", "Bash: b"))
        w._publish()
        sends = [kw["text"] for m, kw in self.sent if m == "sendMessage"]
        self.assertEqual(len(sends), 2)
        self.assertTrue("One." in sends[0])
        self.assertTrue("Two." in sends[1])
        edits = {kw["message_id"]: kw["text"] for m, kw in self.sent
                 if m == "editMessageText"}
        self.assertIn("Bash: a", edits[1])
        self.assertNotIn("Bash: b", edits[1])
        self.assertIn("Two.", edits[2])
        self.assertIn("Bash: b", edits[2])
        self.assertNotIn("Bash: a", edits[2])

    def test_long_progress_keeps_formatted_narration_and_fits_utf16_limit(self):
        import html
        import re
        w = self.w
        prose = "**Checking** " + "x" * 3650
        w.on_progress(think.Progress("text", prose))
        w.on_progress(think.Progress("tool", "<command> " + "😀" * 2000))
        w._publish()
        rendered = self.sent[-1][1]["text"]
        self.assertIn("<b>Checking</b>", rendered)
        self.assertIn("x" * 3650, rendered)
        self.assertIn("earlier steps trimmed", rendered)
        plain = html.unescape(re.sub(r"<[^>]*>", "", rendered))
        self.assertLessEqual(len(plain.encode("utf-16-le")) // 2, 4096)
        self.assertEqual(len(self.texts()), 1)

    def test_multichunk_narration_edits_only_its_last_message(self):
        w = self.w
        w.on_progress(think.Progress("text", "😀" * 3900))
        w.on_progress(think.Progress("tool", "Read: last.py"))
        initial = len(self.texts())
        self.assertGreater(initial, 1)
        w._publish()
        self.assertEqual(len(self.texts()), initial)
        self.assertEqual(self.sent[-1][1]["message_id"], w.message_ids[-1])
        self.assertIn("last.py", self.sent[-1][1]["text"])

    def test_rendered_markdown_expansion_is_included_in_length_budget(self):
        w = self.w
        # Horizontal rules grow from three source characters to six displayed.
        w.on_progress(think.Progress("text", "---\n\n" * 750))
        w.on_progress(think.Progress("tool", "Read: " + "x" * 5000))
        w._publish()
        for method, params in self.sent:
            if method in ("sendMessage", "editMessageText"):
                self.assertLessEqual(w._display_units(params["text"]), 4096)

    def test_failed_interim_send_is_not_marked_as_delivered(self):
        self.mod.api = lambda *args, **kw: {"ok": False}
        self.assertFalse(self.w.on_progress(think.Progress("interim", "Keep this reply.")))
        self.assertFalse(self.w.already_sent("Keep this reply."))

    def test_the_final_answer_is_not_sent_by_the_transcript(self):
        w = self.w
        w.on_progress(think.Progress("tool", "Read: a.py"))
        w.on_progress(think.Progress("text", "The answer is four."))
        w.settle("The answer is four.")
        self.assertFalse(any("answer is four" in t.lower() for t in self.texts()))
        self.assertIsNone(w._pending)

    def test_a_failed_turn_does_not_lose_what_it_last_said(self):
        w = self.w
        w.on_progress(think.Progress("text", "I was about to try the second approach."))
        w.settle("", stopped=True)
        self.assertTrue(any("second approach" in t for t in self.texts()))

    def test_a_block_that_is_not_the_answer_is_still_sent_at_the_end(self):
        w = self.w
        w.on_progress(think.Progress("text", "Let me look into that."))
        w.settle("Here is something quite different.")
        self.assertTrue(any("Let me look into that." in t for t in self.texts()))

    def test_two_blocks_in_a_row_send_the_first(self):
        w = self.w
        w.on_progress(think.Progress("text", "First thing."))
        w.on_progress(think.Progress("text", "Second thing."))
        self.assertTrue(any("First thing." in t for t in self.texts()))
        self.assertFalse(any("Second thing." in t for t in self.texts()))

    def test_a_draft_sent_back_by_the_stop_hook_is_never_sent(self):
        # 13 Nov 2026: the prose hook refused a reply, the rewrite followed,
        # and the bridge sent both because the rewrite looked like a second block.
        w = self.w
        w.on_progress(think.Progress("text", "You're right about the draft."))
        w.on_progress(think.Progress("retract", "Stop hook feedback: ..."))
        w.on_progress(think.Progress("text", "The rewritten answer."))
        w.settle("The rewritten answer.")
        self.assertFalse(any("right about the draft" in t for t in self.texts()))

    def test_reasoning_stays_in_the_block(self):
        w = self.w
        w.on_progress(think.Progress("reasoning", "Weighing two options."))
        self.assertEqual(self.texts(), [])
        self.assertIn("Weighing two options.", w._render())

    def test_an_interim_reply_is_not_sent_twice(self):
        w = self.w
        w.on_progress(think.Progress("tool", "Agent: x"))
        w.on_progress(think.Progress("text", "First half, held."))
        w.on_progress(think.Progress("interim", "First half, held."))
        self.assertEqual(len([t for t in self.texts() if "First half, held." in t]), 1)

    def test_the_setting_chooses_the_mode(self):
        self.assertEqual(self.mod.Working(chat_id=1).narration, "messages")


class TypingIndicator(unittest.TestCase):
    """The General topic carries no thread id, but its indicator needs one."""

    def _typing(self, chat_id, thread_id=None, responses=()):
        src = (ROOT / "bin" / "herald-telegram").read_text()
        mod = types.ModuleType("tg")
        mod.__dict__["__file__"] = str(ROOT / "bin" / "herald-telegram")
        exec(compile(src.split("def main()")[0], "herald-telegram", "exec"),
             mod.__dict__)
        calls, replies = [], list(responses)

        def api(method, **params):
            calls.append((method, params.get("message_thread_id")))
            return replies.pop(0) if replies else {"ok": True}

        mod.api = api
        return mod.Working(chat_id=chat_id, thread_id=thread_id), calls

    def test_general_topic_is_addressed_explicitly(self):
        w, calls = self._typing(-100)
        w._send_typing()
        self.assertEqual(calls, [("sendChatAction", 1)])

    def test_a_named_topic_keeps_its_own_id(self):
        w, calls = self._typing(-100, thread_id=9)
        w._send_typing()
        self.assertEqual(calls, [("sendChatAction", 9)])

    def test_a_direct_chat_sends_no_id(self):
        w, calls = self._typing(42)
        w._send_typing()
        self.assertEqual(calls, [("sendChatAction", None)])

    def test_a_group_without_general_falls_back_and_stops_trying(self):
        w, calls = self._typing(-100, responses=[None])
        w._send_typing()
        w._send_typing()
        self.assertEqual(calls, [("sendChatAction", 1), ("sendChatAction", None),
                                 ("sendChatAction", None)])


class CodexReasoningIsNotAMessage(unittest.TestCase):
    def test_reasoning_items_have_their_own_kind(self):
        seen = []
        state: dict = {}
        for item in ({"type": "reasoning", "text": "Thinking about it."}, {"type": "agent_message", "text": "Done."}):
            think._handle_codex_line(json.dumps({"type": "item.completed", "item": {"id": "i", **item}}), state, seen.append)
        self.assertEqual([(e.kind, e.text) for e in seen], [("reasoning", "Thinking about it."), ("text", "Done.")])


class StaleNotificationResult(unittest.TestCase):
    """A resumed session answers a notification the last process left before
    it reads the new prompt. That result is not this run's answer."""

    def _feed(self, state, **result):
        think._handle_stream_line(json.dumps({"type": "result", **result}),
                                  state, None)

    def test_a_drained_notification_does_not_end_the_turn(self):
        state: dict = {"held_payload": None}
        self._feed(state, origin={"kind": "task-notification"}, result="", num_turns=0)
        self.assertIsNone(state.get("payload"))
        self._feed(state, result="the answer", num_turns=4)
        self.assertEqual(state["payload"]["result"], "the answer")

    def test_a_notification_after_a_held_answer_still_counts(self):
        """This run's own background task finishing is the real end."""
        state: dict = {"held_payload": {"result": "waiting"}}
        self._feed(state, origin={"kind": "task-notification"}, result="done")
        self.assertEqual(state["payload"]["result"], "done")


if __name__ == "__main__":
    unittest.main()
