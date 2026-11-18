"""Account ownership must survive quota changes, slot moves and worker restarts."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))
from herald import claude_accounts as ca, config, think, routing, usage, limitchoice


def account(number, pct, *, active=False, week=10):
    return {"number": number, "email": f"account{number}@example.com", "organizationUuid": f"org-{number}",
            "active": active, "usageStatus": "ok", "usageAgeSeconds": 1,
            "plan": {"label": "Pro", "expired": False},
            "usage": {"fiveHour": {"pct": pct, "resetsAt": "2099-01-01T12:00:00Z"},
                      "sevenDay": {"pct": week, "resetsAt": "2099-01-07T12:00:00Z"}}}


class Accounts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patch = mock.patch.object(config, "RAW", pathlib.Path(self.tmp.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.rows = [account(1, 98, active=True, week=57), account(2, 0)]

    def choose(self, resume=None, model="opus"):
        with mock.patch.object(ca, "accounts", return_value=self.rows):
            return ca.select(resume, model)

    def test_new_session_uses_second_account_at_threshold(self):
        self.assertEqual(self.choose()["number"], 2)
        self.rows[0]["usage"]["fiveHour"]["pct"] = 50
        self.rows[0]["usage"]["sevenDay"]["pct"] = 10
        self.assertEqual(self.choose()["number"], 2)

    def test_new_conversation_balances_instead_of_favoring_default(self):
        self.rows[0]["usage"]["fiveHour"]["pct"] = 49
        self.rows[0]["usage"]["sevenDay"]["pct"] = 10
        self.assertEqual(self.choose()["number"], 2)

    def test_week_and_applicable_model_limit_count(self):
        self.rows[0]["usage"]["fiveHour"]["pct"] = 0
        self.assertEqual(self.choose()["number"], 2)
        self.rows[0]["usage"]["sevenDay"]["pct"] = 10
        self.rows[0]["usage"]["scoped"] = [{"name": "Opus", "pct": 100, "resetsAt": "2099-01-07T12:00:00Z"}]
        self.assertEqual(self.choose(model="claude-opus-4-6")["number"], 2)
        self.assertEqual(self.choose(model="haiku")["number"], 1)

    def test_resumed_session_ignores_threshold_and_slot_move(self):
        ca.bind("native-session", self.rows[1])
        self.rows[1]["number"] = 9
        self.rows[1]["usage"]["fiveHour"]["pct"] = 100
        self.assertEqual(self.choose("native-session")["email"], "account2@example.com")
        self.assertEqual(ca.binding("native-session")["email"], "account2@example.com")

    def test_legacy_resume_adopts_default_without_balancing(self):
        self.assertEqual(self.choose("old-session")["number"], 1)
        self.rows[0]["active"], self.rows[1]["active"] = False, True
        self.assertEqual(self.choose("old-session")["number"], 1)

    def test_missing_bound_identity_does_not_fall_back(self):
        ca.bind("native-session", self.rows[1])
        self.rows.pop()
        with self.assertRaises(ca.AccountError):
            self.choose("native-session")

    def test_cannot_overwrite_ownership(self):
        ca.bind("native-session", self.rows[0])
        with self.assertRaises(ca.AccountError):
            ca.bind("native-session", self.rows[1])

    def test_only_turn_launch_can_move_near_quota(self):
        ca.bind("native-session", self.rows[0])
        with mock.patch.object(ca, "accounts", return_value=self.rows):
            self.assertEqual(ca.select("native-session", "opus")["number"], 1)
            other = ca.select("native-session", "opus", allow_move=True)
        self.assertEqual(other["number"], 2)
        self.assertIn("_herald_move", other)
        self.assertEqual(ca.binding("native-session")["email"], self.rows[1]["email"])

    def test_no_move_below_90_or_without_20_point_improvement(self):
        ca.bind("native-session", self.rows[0])
        with mock.patch.object(ca, "accounts", return_value=self.rows):
            self.rows[0]["usage"]["fiveHour"]["pct"] = 89
            self.assertEqual(ca.select("native-session", "opus", allow_move=True)["number"], 1)
            self.rows[0]["usage"]["fiveHour"]["pct"] = 90
            self.rows[1]["usage"]["fiveHour"]["pct"] = 71
            self.assertEqual(ca.select("native-session", "opus", allow_move=True)["number"], 1)
            self.rows[1]["usage"]["fiveHour"]["pct"] = 70
            self.assertEqual(ca.select("native-session", "opus", allow_move=True)["number"], 2)

    def test_move_cooldown_survives_next_selection(self):
        ca.bind("native-session", self.rows[0])
        with mock.patch.object(ca, "accounts", return_value=self.rows), \
             mock.patch.object(ca.time, "time", return_value=1_800_000_000) as now:
            self.assertEqual(ca.select("native-session", "opus", allow_move=True)["number"], 2)
            self.rows[1]["usage"]["fiveHour"]["pct"] = 98
            self.rows[0]["usage"]["fiveHour"]["pct"] = 0
            self.rows[0]["usage"]["sevenDay"]["pct"] = 10
            now.return_value += 60
            self.assertEqual(ca.select("native-session", "opus", allow_move=True)["number"], 2)
            now.return_value += 1800
            self.assertEqual(ca.select("native-session", "opus", allow_move=True)["number"], 1)

    def test_stale_owner_never_triggers_cache_move(self):
        ca.bind("native-session", self.rows[0])
        self.rows[0]["usageStatus"] = "error"
        with mock.patch.object(ca, "accounts", return_value=self.rows):
            self.assertEqual(ca.select("native-session", "opus", allow_move=True)["number"], 1)

    def test_stale_unknown_disabled_expired_and_invalid_not_selected(self):
        bad_values = [{"usageStatus": "error"}, {"usageAgeSeconds": 901},
                      {"usageAgeSeconds": float("nan")}, {"disabled": True},
                      {"plan": {"expired": True}}, {"usage": {}},
                      {"usage": {"sevenDay": {"pct": float("nan")}}},
                      {"usage": {"sevenDay": {"pct": 10, "resetsAt": "2000-01-01T00:00:00Z"}}}]
        for values in bad_values:
            with self.subTest(values=values):
                row = account(2, 0)
                row.update(values)
                self.rows = [row]
                with self.assertRaises(ca.AccountError):
                    self.choose()

    def test_if_both_over_threshold_choose_less_used(self):
        self.rows[1]["usage"]["fiveHour"]["pct"] = 70
        self.assertEqual(self.choose()["number"], 2)
        self.rows[1]["usage"]["fiveHour"]["pct"] = 100
        self.rows[0]["usage"]["fiveHour"]["pct"] = 100
        with self.assertRaises(ca.AccountError):
            self.choose()

    def test_cached_report_makes_no_subprocess_call(self):
        payload = {"schemaVersion": 1, "accounts": self.rows}
        with mock.patch.object(ca.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=json.dumps(payload))):
            ca.accounts()
        with mock.patch.object(ca.subprocess, "run", side_effect=AssertionError("unexpected CLI call")):
            text, failed = ca.report(cached_only=True)
        self.assertFalse(failed)
        self.assertIn("account2@example.com", text)

    def test_report_converts_reset_to_local_time(self):
        with mock.patch.object(ca, "accounts", return_value=self.rows), \
             mock.patch.object(config, "tz", return_value=ZoneInfo("America/New_York")):
            text, _ = ca.report()
        self.assertIn("7:00", text)
        self.assertNotIn("12:00", text)

    def test_cli_error_does_not_echo_output(self):
        with mock.patch.object(ca.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="secret", stderr="secret")):
            with self.assertRaises(ca.AccountError) as err:
                ca.accounts()
        self.assertNotIn("secret", str(err.exception))

    def test_malformed_cli_json_fails_with_an_account_error(self):
        for payload in ([], {"schemaVersion":1,"accounts":[{}]}, {"schemaVersion":2,"accounts":[]}):
            with self.subTest(payload=payload), \
                 mock.patch.object(ca.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=json.dumps(payload))):
                with self.assertRaises(ca.AccountError):
                    ca.accounts()

    def test_ambiguous_email_cannot_launch_wrong_organization(self):
        self.rows[0]["email"] = self.rows[1]["email"]
        with self.assertRaises(ca.AccountError):
            self.choose()

    def test_inherited_oauth_cannot_override_selected_login(self):
        with mock.patch.dict(think.os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "test-only", "HERALD_WORKER_PARENT": ""}), \
             mock.patch.object(think.subprocess, "Popen") as spawn:
            think._spawn("claude", ["cswap", "run", "account2@example.com", "--", "-p"])
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", spawn.call_args.kwargs["env"])

    def test_launch_binds_before_checkpoint_and_preserves_claude_flags(self):
        seen = []
        def run(engine, cmd, prompt, **kwargs):
            self.assertEqual(cmd[:5], ["cswap", "run", "account2@example.com", "--share-history", "--"])
            self.assertIn("stream-json", cmd)
            self.assertIn("--effort", cmd)
            kwargs["on_progress"](think.Progress("session", "launched-session"))
            self.assertTrue(kwargs["on_progress"](think.Progress("interim", "waiting")))
            return (0, {"session_id": "launched-session", "result": "ok"}, None, "", "", False)
        def checkpoint(progress):
            if progress.kind == "session":
                seen.append(ca.binding(progress.text))
            return True
        with mock.patch.object(ca, "enabled", return_value=True), \
             mock.patch.object(ca, "accounts", return_value=self.rows), \
             mock.patch.object(think, "_run_process", side_effect=run):
            result = self.launch(checkpoint)
        self.assertEqual(result[0], 0)
        self.assertEqual(seen[0]["email"], "account2@example.com")

    def launch(self, on_progress=None, lean=False):
        return think._run_claude("hello", model="haiku", effort="low", cwd=pathlib.Path(self.tmp.name),
            timeout=20, idle_timeout=20, allowed_tools=None, append_system_prompt=None,
            json_schema=None, resume=None, permission_mode="plan", add_dirs=None,
            on_progress=on_progress, lean=lean)

    def test_private_and_lean_calls_do_not_read_or_write_account_state(self):
        for private, lean in ((pathlib.Path(self.tmp.name), False), (None, True)):
            with self.subTest(private=private, lean=lean), \
                 mock.patch.object(ca, "enabled", return_value=True), \
                 mock.patch.object(think, "_private_scratch", return_value=private), \
                 mock.patch.object(ca, "select", side_effect=AssertionError("account state accessed")), \
                 mock.patch.object(think, "_run_process", return_value=(0, {}, None, "", "", False)) as run:
                self.launch(lean=lean)
                self.assertEqual(run.call_args.args[1][0], "claude")

    def test_auto_does_not_treat_default_login_as_topic_quota(self):
        with mock.patch.object(ca, "enabled", return_value=True), \
             mock.patch.object(routing, "_limit_until", side_effect=AssertionError("wrong account")):
            self.assertIsNone(routing.before_turn(None, "claude"))

    def test_usage_report_uses_account_readings(self):
        with mock.patch.object(ca, "enabled", return_value=True), \
             mock.patch.object(ca, "accounts", return_value=self.rows), \
             mock.patch.object(config, "enabled_engines", return_value=("claude",)), \
             mock.patch.object(usage.anchors, "describe", return_value=""):
            text, failed = usage.report(None)
        self.assertFalse(failed)
        self.assertIn("account2@example.com", text)

    def test_limit_card_uses_bound_account_reset(self):
        ca.bind("native-session", self.rows[1])
        self.rows[1]["usage"]["fiveHour"] = {"pct": 100, "resetsAt": "2099-01-01T12:00:00Z"}
        with mock.patch.object(ca, "enabled", return_value=True), \
             mock.patch.object(ca, "accounts", return_value=self.rows), \
             mock.patch.object(config, "enabled_engines", return_value=("claude",)), \
             mock.patch.object(limitchoice.limitresume, "reset_at", side_effect=AssertionError("wrong quota source")):
            record, text, _ = limitchoice.make(None, {"engine": "claude", "session_id": "native-session", "last_ts": 1})
        self.assertGreater(record["at"], 4_000_000_000)
        self.assertNotIn("couldn't verify", text)

    def test_capacity_uses_second_account_when_default_at_limit(self):
        self.rows[0]["usage"]["fiveHour"]["pct"] = 100
        with mock.patch.object(ca, "enabled", return_value=True), \
             mock.patch.object(ca, "accounts", return_value=self.rows), \
             mock.patch.object(config, "enabled_engines", return_value=("claude",)):
            snapshot = limitchoice.capacity(None, "claude")
        self.assertEqual(snapshot["account"], "account2@example.com")


if __name__ == "__main__":
    unittest.main()
