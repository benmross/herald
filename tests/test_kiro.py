"""Native V3 event shapes captured from Kiro CLI 3.3.0, without personal data."""
import json
import pathlib
import sys
import tempfile
import unittest
import sqlite3
import datetime as dt
from contextlib import contextmanager
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "lib"), str(ROOT / "tools")]
from herald import config, db, kiro, think, usage
import kiro_hook


def feed(state, update):
    think._handle_kiro_line(json.dumps({"type": "sessionUpdate", "data": {
        "sessionId": "sess_test", "update": update}}), state, None)


class KiroTests(unittest.TestCase):
    def test_live_native_catalog_supplies_effort_levels_without_prompt(self):
        sent = []
        @contextmanager
        def process(cmd):
            self.assertEqual(cmd[1:], ['acp', '--agent-engine', 'v3'])
            def ask(messages, matches, observe=None):
                sent.extend(messages)
                request = messages[-1]
                result = {} if request['method'] == 'initialize' else {'configOptions': [{
                    'id': 'model', 'options': [{'value': 'opus', '_meta': {'kiro': {
                        'hasEffort': True, 'effortLevels': ['high', 'max'], 'defaultEffortLevel': 'high'}}},
                        {'value': 'auto', '_meta': {'kiro': {'hasEffort': False}}}]}]}
                return {'id': request['id'], 'result': result}
            yield ask
        with mock.patch.object(think, '_catalog_process', process), mock.patch.object(think, '_kiro_caps_cache', {}):
            caps = think._kiro_model_capabilities()
        self.assertEqual(caps['opus']['efforts'], ['high', 'max'])
        self.assertEqual(caps['auto']['efforts'], [])
        self.assertEqual([m['method'] for m in sent], ['initialize', 'session/new'])

    def test_missing_capability_data_is_unknown_not_unadjustable(self):
        response = mock.Mock(returncode=0, stdout=json.dumps({'models': [
            {'model_id': 'opus', 'model_name': 'Opus'}]}))
        with mock.patch.object(think, 'resolve_engine'), mock.patch.object(think.subprocess, 'run', return_value=response), \
                mock.patch.object(think, '_kiro_model_capabilities', side_effect=RuntimeError('unavailable')):
            row = think.available_models('kiro')[0]
        self.assertNotIn('efforts', row)

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

    def test_duplicate_native_receipts_count_once_and_replay_is_ignored(self):
        state = {}
        update = {'sessionUpdate': 'session_info_update', '_meta': {'kiro': {
            'kind': 'turn_completion', 'executionId': 'turn1',
            'promptTurnSummaries': [{'unit': 'credit', 'usage': 0.5}]}}}
        feed(state, update)
        feed(state, update)
        self.assertEqual(state['credits_used'], 0.5)
        update['_meta']['kiro'].update(executionId='old', replay=True)
        feed(state, update)
        self.assertEqual(state['credits_used'], 0.5)

    def test_every_outcome_keeps_native_credits_including_timeout_without_payload(self):
        for outcome, code, payload, cancelled in (
                ('success', 0, {'result': 'ok'}, False), ('failed', 1, {'is_error': True}, False),
                ('cancelled', -15, None, True), ('timeout', 124, None, False)):
            def launch(*args, **kwargs):
                kwargs['timing']['kiro_usage'] = {'session_id': 'sess_test', 'credits': 0.4, 'turn_ids': ['turn1']}
                return code, payload, None, '', '', cancelled
            with self.subTest(outcome=outcome), mock.patch.object(config, 'enabled_engines', return_value=('kiro',)), \
                    mock.patch.object(think, '_run_kiro', side_effect=launch), mock.patch.object(think, '_record'), \
                    mock.patch('herald.debugtrace.Trace'), \
                    mock.patch.object(kiro, 'record'), mock.patch.object(kiro, 'record_attempt') as record:
                result = think.think('probe', label='test', engine='kiro', model='opus')
            self.assertEqual(record.call_args.kwargs['outcome'], outcome)
            self.assertEqual(record.call_args.kwargs['credits'], 0.4)
            self.assertEqual(result.credits_used, 0.4)

    def test_timeout_keeps_metering_received_before_the_final_result(self):
        timing = {}
        def process(*args, **kwargs):
            state = {}
            event = {'type': 'sessionUpdate', 'data': {'sessionId': 'sess_test', 'update': {
                'sessionUpdate': 'session_info_update', '_meta': {'kiro': {'kind': 'turn_completion',
                    'executionId': 'turn1', 'promptTurnSummaries': [{'unit': 'credit', 'usage': 0.25}]}}}}}
            kwargs['handler'](json.dumps(event), state, None, None)
            return 124, None, None, '', 'timed out', False
        with mock.patch.object(kiro, 'profile', return_value='test'), mock.patch.object(kiro, 'verify_workspace'), \
                mock.patch.object(think, 'available_models', return_value=[]), mock.patch.object(think, '_run_process', side_effect=process):
            think._run_kiro('test', model=None, effort=None, cwd=ROOT, timeout=1, idle_timeout=1,
                resume=None, append_system_prompt=None, json_schema=None, allowed_tools=None,
                permission_mode='auto', timing=timing)
        self.assertEqual(timing['kiro_usage']['credits'], 0.25)
        self.assertEqual(timing['kiro_usage']['session_id'], 'sess_test')

    def test_missing_receipts_are_not_zero_and_account_reconciliation_is_not_attribution(self):
        con = sqlite3.connect(':memory:')
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        con.executescript(db.SCHEMA)
        @contextmanager
        def session():
            yield con
        with mock.patch.object(db, 'session', session):
            for outcome, credits in (('failed', 0.5), ('cancelled', None), ('timeout', 0.0)):
                kiro.record_attempt(label='test', model='opus', session_id='sess_test', credits=credits,
                                    turn_ids=[], outcome=outcome)
        rows = [json.loads(r[0]) for r in con.execute("SELECT data FROM facts WHERE kind='attempt'")]
        self.assertEqual(len(rows), 3)
        missing = next(r for r in rows if r['outcome'] == 'cancelled')
        self.assertIsNone(missing['credits'])
        self.assertEqual(missing['receipt_status'], 'missing')
        self.assertEqual(con.execute("SELECT count(*) FROM facts WHERE kind='consumption'").fetchone()[0], 2)
        now = dt.datetime.now(config.tz())
        reset = (now.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        usage.reconcile_kiro(con, {'sampled_at': now.timestamp(), 'windows': [{
            'resets_at': reset.timestamp(), 'credits_used': 2.0}]})
        row = json.loads(con.execute("SELECT data FROM facts WHERE kind='reconciliation'").fetchone()[0])
        self.assertEqual(row['missing_receipts'], 1)
        self.assertEqual(row['unattributed_credits'], 1.5)
        self.assertEqual(row['account_credits'], 2)

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
        # Installed skill links and optional personal skill paths are absent in
        # a clean checkout. Exercise the hook against an explicit registration.
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            skill = root / ".agents/skills/herald-self/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\nname: herald-self\ndescription: Test skill\n---\n")
            with mock.patch.object(kiro_hook.guard, "ROOT", root), \
                    mock.patch.object(config, "get", return_value=[]):
                for args in ({"name": "herald-self"}, {"id": "skill::herald-self"}):
                    with self.subTest(args=args):
                        event = kiro_hook.normalize({"tool_name": "disclose_context", "tool_input": args})
                        self.assertEqual(event["tool_name"], "Read")
                        self.assertEqual(event["tool_input"]["file_path"], str(skill))
                for name in ("../../secret", "unregistered-skill"):
                    with self.subTest(name=name), self.assertRaises(ValueError):
                        kiro_hook.normalize({"tool_name": "disclose_context", "tool_input": {"name": name}})

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
