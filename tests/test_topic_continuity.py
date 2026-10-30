"""Renewal continues a topic's work, /new abandons it, Auto moves it on a limit."""
import importlib.machinery
import importlib.util
import json
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from herald import handoff, routing, transcripts  # noqa: E402


def _bridge():
    loader = importlib.machinery.SourceFileLoader("tg_continuity", str(ROOT / "bin" / "herald-telegram"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Renewal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tg = _bridge()

    def test_context_share_renews_and_missing_telemetry_keeps_the_ceiling(self):
        ts = {"session_id": "a", "turns": 3, "last_ts": time.time(),
              "context_tokens": 800_000, "context_window": 1_000_000}
        self.assertIn("context", self.tg.should_rotate(ts))
        ts.update(context_tokens=100_000)
        self.assertIsNone(self.tg.should_rotate(ts))
        ts.update(context_tokens=None, context_window=None, turns=self.tg.MAX_TURNS)
        self.assertIn("turns", self.tg.should_rotate(ts))

    def test_a_handoff_clears_the_old_sessions_size_and_keeps_its_work(self):
        ts = {"session_id": "a", "turns": 9, "context_tokens": 9, "context_window": 10,
              "work": {"request": "build it"}}
        self.tg._hand_off(ts, "a")
        self.assertEqual((ts["handoff_from"], ts["session_id"], ts["turns"]), ("a", None, 0))
        self.assertIsNone(ts.get("context_window"))
        self.assertEqual(ts["work"]["request"], "build it")


class Work(unittest.TestCase):
    def test_outcomes_keep_paths_and_command_names_not_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "s.jsonl"
            lines = [{"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Write", "input": {"file_path": "/a.py", "content": "x"}},
                {"type": "tool_use", "name": "Bash", "input": {"command": "git commit -m secret && ls"}},
                {"type": "tool_use", "name": "Bash", "input": {"command": "cat f | herald act email --x"}}]}}]
            path.write_text("\n".join(json.dumps(x) for x in lines))
            done = transcripts.outcomes(path)
        self.assertEqual(done, {"files": ["/a.py"], "actions": ["git commit", "herald act email"]})

    def test_codex_outcomes_read_its_own_shapes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "rollout-x.jsonl"
            lines = [{"type": "response_item", "payload": {"type": "custom_tool_call",
                      "input": "*** Begin Patch\n*** Update File: /b.py\n"}},
                     {"type": "response_item", "payload": {"type": "custom_tool_call",
                      "input": 'text( await tools.exec_command({cmd:"git push origin main"}))'}}]
            path.write_text("\n".join(json.dumps(x) for x in lines))
            self.assertEqual(transcripts.outcomes(path),
                             {"files": ["/b.py"], "actions": ["git push"]})

    def test_the_brief_carries_the_work_and_withholds_approval(self):
        con = sqlite3.connect(":memory:")
        text = handoff.brief(con, None, 0, {"request": "ship", "files": ["/a"], "actions": []})
        self.assertIn("ship", text)
        self.assertIn("/a", text)
        self.assertIn("approvals do not carry over", text)

    def test_with_nothing_to_carry_the_brief_is_empty(self):
        self.assertEqual(handoff.brief(sqlite3.connect(":memory:"), None), "")


class Auto(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute("CREATE TABLE facts (source, kind, external_id, data)")
        self.now = time.time()

    def _reading(self, engine, used, resets, sampled=None):
        self.con.execute("INSERT INTO facts VALUES ('subscription_usage','snapshot',?,?)", (engine, json.dumps(
            {"sampled_at": sampled or self.now, "windows": [{"used_percent": used, "resets_at": resets}]})))

    def _settings(self, **values):
        base = {"engines.auto.enabled": True, "engines.auto.limit_percent": 99,
                "engines.enabled": ["claude", "codex"], "usage.max_age_minutes": 45, **values}
        return mock.patch.object(routing.config, "get", lambda k, d=None: base.get(k, d))

    def test_a_limit_moves_the_topic_and_ordinary_usage_does_not(self):
        self._reading("claude", 100, self.now + 3600)
        self._reading("codex", 10, self.now + 3600)
        with self._settings():
            self.assertEqual(routing.before_turn(self.con, "claude")[0], "codex")
            self.assertIsNone(routing.before_turn(self.con, "codex"))

    def test_both_at_a_limit_or_a_stale_reading_stays(self):
        self._reading("claude", 100, self.now + 3600)
        self._reading("codex", 100, self.now + 3600)
        with self._settings():
            self.assertIsNone(routing.before_turn(self.con, "claude"))
        self.con.execute("DELETE FROM facts")
        self._reading("claude", 100, self.now + 3600, sampled=self.now - 7200)
        with self._settings():
            self.assertIsNone(routing.before_turn(self.con, "claude"))

    def test_off_pinned_and_single_provider_installs_never_move(self):
        with self._settings(**{"engines.auto.enabled": False}):
            self.assertFalse(routing.enabled({}))
        with self._settings():
            self.assertTrue(routing.enabled({}))
            self.assertFalse(routing.enabled({"pinned": True}))
        with self._settings(**{"engines.enabled": ["codex"]}):
            self.assertFalse(routing.enabled({}))


if __name__ == "__main__":
    unittest.main()
