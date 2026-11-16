"""Native V3 event shapes captured from Kiro CLI 3.3.0, without personal data."""
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "lib"), str(ROOT / "tools")]
from herald import config, kiro, think, usage
import kiro_hook


def feed(state, update):
    think._handle_kiro_line(json.dumps({"type": "sessionUpdate", "data": {
        "sessionId": "sess_test", "update": update}}), state, None)


class KiroTests(unittest.TestCase):
    def test_confirmed_completion_and_native_credit_receipt(self):
        state = {"payload": None}
        feed(state, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "ok"}})
        self.assertIsNone(state["payload"])
        feed(state, {"sessionUpdate": "session_info_update", "_meta": {"kiro": {
            "kind": "turn_completion", "promptTurnSummaries": [{"unit": "credit", "usage": 0.33}]}}})
        think._handle_kiro_line(json.dumps({"type": "runFinished", "data": {
            "sessionId": "sess_test", "status": "success", "stopReason": "end_turn", "finalText": "ok"}}), state, None)
        self.assertFalse(state["payload"]["is_error"])
        self.assertEqual(state["payload"]["session_id"], "sess_test")
        self.assertEqual(state["payload"]["credits_used"], 0.33)

    def test_wrong_model_aborts_before_accepting_a_reply(self):
        state = {"expected_model": "claude-opus-5.5", "kiro_model": "auto"}
        feed(state, {"sessionUpdate": "session_info_update", "_meta": {"kiro": {"turnStart": True}}})
        self.assertTrue(state["abort"])
        self.assertTrue(state["payload"]["is_error"])

    def test_wrong_effort_also_aborts(self):
        state = {"expected_effort": "high", "kiro_effortLevel": "medium"}
        feed(state, {"sessionUpdate": "session_info_update", "_meta": {"kiro": {"turnStart": True}}})
        self.assertTrue(state["abort"])

    def test_interruption_is_not_success(self):
        state = {}
        think._handle_kiro_line(json.dumps({"type": "runFinished", "data": {
            "status": "interrupted", "stopReason": "cancelled"}}), state, None)
        self.assertTrue(state["payload"]["is_error"])

    def test_partial_stream_keeps_native_resume_id(self):
        line = json.dumps({"type": "sessionUpdate", "data": {"sessionId": "sess_test"}})
        self.assertEqual(think._session_from_stream(line), "sess_test")

    def test_native_shell_gets_existing_google_guard(self):
        event = kiro_hook.normalize({"tool_name": "execute_bash", "tool_input": {
            "command": "python3 -c 'if False: s.users().messages().send().execute()'"}})
        self.assertFalse(kiro_hook.guard.decide_google(event)[0])

    def test_unknown_tools_and_background_work_fail_closed(self):
        for name, args in (("invoke_sub_agent", {}), ("execute_bash", {"command": "echo ok", "run_in_background": True}),
                           ("read_file", {}), ("mcp_anything", {})):
            with self.assertRaises(ValueError):
                kiro_hook.normalize({"tool_name": name, "tool_input": args})

    def test_native_skill_loader_uses_name_in_hook_and_id_in_stream(self):
        event = kiro_hook.normalize({"tool_name": "disclose_context", "tool_input": {"name": "herald-self"}})
        self.assertEqual(event["tool_name"], "Read")
        self.assertTrue(event["tool_input"]["file_path"].endswith("herald-self/SKILL.md"))
        with self.assertRaises(ValueError):
            kiro_hook.normalize({"tool_name": "disclose_context", "tool_input": {"name": "../../secret"}})

    def test_profiles_are_guarded_in_every_working_directory_and_policy_is_distinct(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(kiro.Path, "home", return_value=pathlib.Path(td)):
            name = kiro.profile()
            profile = json.loads((pathlib.Path(td) / ".kiro/agents" / (name + ".json")).read_text())
            self.assertFalse(profile["includeMcpJson"])
            self.assertFalse(profile["includePowers"])
            self.assertNotIn("invoke_sub_agent", profile["tools"])
            self.assertEqual(profile["hooks"][0]["trigger"], "PreToolUse")
            self.assertIn("kiro_hook.py", profile["hooks"][0]["action"]["command"])
            self.assertNotEqual(name, kiro.profile(no_tools=True))
            self.assertNotEqual(kiro.profile(lean=True), kiro.profile(no_tools=True))
            workspace = pathlib.Path(td) / "project"
            override = workspace / ".kiro/agents" / (name + ".json")
            override.parent.mkdir(parents=True)
            override.write_text('{}')
            with self.assertRaises(RuntimeError):
                kiro.verify_workspace(name, workspace)

    def test_credit_pane_is_strict(self):
        result = usage.parse_kiro("Estimated Usage | resets on 2026-12-01 | KIRO STUDENT\nCredits (12.50 of 1000 covered in plan)")
        self.assertEqual(result[0]["credits_total"], 1000)
        self.assertEqual(result[0]["used_percent"], 1.25)
        for text in ("", "Credits (12 of 1000 covered in plan)", "Estimated Usage | resets on 2026-12-01\nCredits (1 of 0 covered in plan)"):
            with self.assertRaises(usage.UsageParseError):
                usage.parse_kiro(text)

    def test_legacy_installs_do_not_silently_enable_kiro(self):
        with mock.patch.object(config, "get", return_value=None):
            self.assertEqual(config.enabled_engines(), ("claude", "codex"))

    def test_private_session_never_starts_kiro(self):
        with mock.patch.object(think, "resolve_engine", return_value="kiro"), mock.patch.object(think, "_spawn") as spawn:
            result = think.think("private", label="test", engine="kiro", private="/tmp/private")
        self.assertFalse(result.ok)
        spawn.assert_not_called()

    def test_api_key_cannot_override_subscription_login(self):
        with mock.patch.dict("os.environ", {"KIRO_API_KEY": "test-only"}):
            self.assertNotIn("KIRO_API_KEY", config.agent_env())

    def test_credit_exhaustion_is_a_limit_without_changing_engines(self):
        self.assertTrue(think._looks_rate_limited(1, None, "Insufficient credits"))
        self.assertFalse(think._looks_rate_limited(0, {"result": "No credit limit reached"}, ""))
