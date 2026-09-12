"""A turn's phase timeline has to survive the parser it is derived from.

The instrumentation is free precisely because it piggybacks on the one pass
`think._handle_stream_line` already makes over the stream. That also makes it
easy to break silently: a phase attributed to the wrong kind, or a tool span
measured from the wrong end, produces numbers that look plausible and point an
optimisation session at the wrong thing. These tests pin the attribution.

The specific mistakes guarded against here were all live at some point while
this was being written: counting the replayed user prompt as a tool result,
recording a zero-length trailing model phase because `result` lands in the same
millisecond as the final `assistant` event, and closing a tool span against the
wrong pending id when two tools are issued in one message.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import think  # noqa: E402


def _feed(state: dict, obj: dict) -> None:
    think._handle_stream_line(json.dumps(obj), state, None)


def _fresh() -> dict:
    return {"payload": None, "last_assistant_usage": None,
            "t0": time.monotonic(), "mark": None, "saw_init": False,
            "phases": [], "pending_tools": {}, "round_trips": 0}


def _kinds(state: dict) -> list[str]:
    return [p["kind"] for p in state["phases"]]


class PhaseAttribution(unittest.TestCase):
    def test_a_whole_turn_lands_in_the_right_phases(self):
        state = _fresh()
        _feed(state, {"type": "system", "subtype": "init"})
        # --replay-user-messages echoes the prompt back. It is not a tool
        # result and must not close anything.
        _feed(state, {"type": "user", "message": {
            "content": [{"type": "text", "text": "do a thing"}]}})
        _feed(state, {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": "echo hi"}}]}})
        _feed(state, {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1"}]}})
        _feed(state, {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "done"}]}})
        _feed(state, {"type": "result", "subtype": "success"})

        self.assertEqual(_kinds(state), ["startup", "model", "tool", "model"])
        self.assertEqual(state["round_trips"], 2)
        tool = [p for p in state["phases"] if p["kind"] == "tool"][0]
        self.assertEqual(tool["name"], "Bash")
        self.assertIn("echo hi", tool["detail"])

    def test_the_replayed_prompt_is_not_mistaken_for_a_tool_result(self):
        state = _fresh()
        _feed(state, {"type": "user", "message": {
            "content": [{"type": "text", "text": "hello"}]}})
        self.assertEqual(state["phases"], [])

    def test_result_does_not_add_an_empty_trailing_phase(self):
        state = _fresh()
        _feed(state, {"type": "assistant",
                      "message": {"content": [{"type": "text", "text": "hi"}]}})
        _feed(state, {"type": "result", "subtype": "success"})
        self.assertEqual(_kinds(state), ["model"])
        self.assertIsNotNone(state["payload"])

    def test_parallel_tools_each_close_against_their_own_span(self):
        state = _fresh()
        _feed(state, {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "Read",
             "input": {"file_path": "x.py"}},
            {"type": "tool_use", "id": "b", "name": "Grep",
             "input": {"pattern": "foo"}}]}})
        _feed(state, {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "b"}]}})
        _feed(state, {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "a"}]}})
        tools = [p["name"] for p in state["phases"] if p["kind"] == "tool"]
        self.assertEqual(tools, ["Grep", "Read"])
        self.assertEqual(state["pending_tools"], {})

    def test_startup_is_recorded_once_even_if_init_repeats(self):
        state = _fresh()
        _feed(state, {"type": "system", "subtype": "init"})
        _feed(state, {"type": "system", "subtype": "init"})
        self.assertEqual(_kinds(state), ["startup"])

    def test_a_malformed_line_changes_nothing(self):
        state = _fresh()
        think._handle_stream_line("not json at all", state, None)
        think._handle_stream_line(json.dumps([1, 2, 3]), state, None)
        self.assertEqual(state["phases"], [])
        self.assertEqual(state["round_trips"], 0)

    def test_progress_still_fires_alongside_the_timing(self):
        """The timing was bolted onto the progress parser; the original job
        has to keep working."""
        seen = []
        state = _fresh()
        think._handle_stream_line(json.dumps({"type": "assistant", "message": {
            "content": [{"type": "text", "text": "thinking"},
                        {"type": "tool_use", "id": "z", "name": "Bash",
                         "input": {"command": "ls"}}]}}), state, seen.append)
        self.assertEqual([p.kind for p in seen], ["text", "tool"])
        self.assertEqual(seen[0].text, "thinking")


if __name__ == "__main__":
    unittest.main()
