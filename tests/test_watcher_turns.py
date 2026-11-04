"""A Monitor left armed does not keep an answered turn open; background work does."""
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from herald import think


def _feed(state, *events):
    for e in events:
        think._handle_stream_line(json.dumps(e), state, None, None)


def _tool(uid, name):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": uid, "name": name, "input": {"command": "x"}}]}}


def _result(uid, text):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": uid, "content": [{"type": "text", "text": text}]}]}}


def _tasks(*ids):
    return {"type": "system", "subtype": "background_tasks_changed",
            "tasks": [{"task_id": i, "task_type": "local_bash", "description": i} for i in ids]}


class Watchers(unittest.TestCase):
    def setUp(self):
        self.state = {"payload": None, "phases": [], "pending_tools": {}, "round_trips": 0,
                      "bg_tasks": [], "held_payload": None, "t0": None}

    def test_background_bash_holds_the_result(self):
        _feed(self.state, _tool("u1", "Bash"),
              _result("u1", "Command running in background with ID: job1."), _tasks("job1"))
        self.assertTrue(think._waiting_on_work(self.state))

    def test_a_monitor_alone_does_not(self):
        _feed(self.state, _tool("u1", "Bash"), _result("u1", "Command running in background"),
              _tool("u2", "Monitor"),
              _result("u2", "Monitor started (task mon7, expires in 25m)."),
              _tasks("job1", "mon7"))
        self.assertTrue(think._waiting_on_work(self.state))
        _feed(self.state, _tasks("mon7"))
        self.assertFalse(think._waiting_on_work(self.state))

    def test_nothing_running(self):
        _feed(self.state, _tasks())
        self.assertFalse(think._waiting_on_work(self.state))


if __name__ == "__main__":
    unittest.main()
