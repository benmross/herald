"""Quota parsing fails visibly when vendor fields or terminal layouts change."""
import datetime as dt
import importlib.machinery
import importlib.util
import itertools
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))
from herald import config, db, think, usage

NOW = dt.datetime(2026, 10, 20, 17, 30, tzinfo=dt.timezone.utc)
# Layout captured from the real interactive CLI; values and timezone changed.
SCREEN = """
   Settings  Status   Config   Usage   Stats
   Usage:                 0 input, 0 output, 0 cache read, 0 cache write
   Current session
   ██████                                             12% used
   Resets 10:20pm (UTC)
   Current week (all models)
   ██████████████████████▌                            45% used
   Resets Oct 23, 11am (UTC)
   What's contributing to your limits usage?
   67%  Start fresh when you switch tasks
   Skills                  % of usage
   /example                       7%
   Usage credits
   Usage credits are off
"""


class Parsers(unittest.TestCase):
    def test_real_layout_ignores_unrelated_percentages(self):
        session, week = usage.parse_claude(SCREEN, NOW)
        self.assertEqual([session['used_percent'], week['used_percent']], [12, 45])
        self.assertEqual(session['resets_at'], int(NOW.replace(hour=22, minute=20).timestamp()))
        self.assertEqual(week['resets_at'], int(NOW.replace(day=23, hour=11, minute=0).timestamp()))

    def test_session_not_started_between_windows(self):
        for idle in ("   0% used\n", ""):
            screen = SCREEN.replace("   ██████                                             12% used\n   Resets 10:20pm (UTC)\n", idle)
            with self.subTest(idle=idle):
                session, week = usage.parse_claude(screen, NOW)
                self.assertEqual((session['used_percent'], session['resets_at']), (0, None))
                self.assertEqual(week['used_percent'], 45)
        with self.assertRaisesRegex(usage.UsageParseError, "saw: 12% used"):
            usage.parse_claude(SCREEN.replace("   Resets 10:20pm (UTC)\n", ""), NOW)

    def test_extra_model_quota_and_remaining_word(self):
        extra = '\nCurrent week (Sonnet only)\n20% remaining\nResets Oct 23, 11am (UTC)\n'
        windows = usage.parse_claude(SCREEN.replace("   What's contributing", extra + "   What's contributing"), NOW)
        self.assertEqual(windows[-1]['used_percent'], 80)

    def test_layout_changes_fail_instead_of_reporting_zero(self):
        for screen in ('', SCREEN.replace('Current session', 'Session allowance'),
                       SCREEN.replace('12% used', '12 percent used'),
                       SCREEN.replace('12% used', '120% used'),
                       SCREEN.replace('10:20pm', 'sometime tonight'),
                       SCREEN.replace('Current session', 'Current day'),
                       SCREEN + '\nCurrent session\n0% used\nResets 10pm (UTC)',
                       SCREEN.replace('Resets Oct 23, 11am (UTC)', ''),
                       SCREEN.replace('Oct 23', 'Oct 19'),
                       SCREEN.replace('10:20pm', '11:55pm')):
            with self.subTest(screen=screen), self.assertRaisesRegex(usage.UsageParseError, 'Cannot parse Claude /usage pane'):
                usage.parse_claude(screen, NOW)

    def test_cached_vendor_screen_is_explicit_error(self):
        with self.assertRaisesRegex(usage.UsageError, 'last-known'):
            usage.parse_claude(SCREEN + '\nShowing last-known usage from 5 minutes ago', NOW)

    def test_weekday_and_year_rollover_resets(self):
        self.assertEqual(dt.datetime.fromtimestamp(usage._reset('Fri 11am (UTC)', NOW), dt.timezone.utc).day, 23)
        december = NOW.replace(month=12, day=31)
        self.assertEqual(dt.datetime.fromtimestamp(usage._reset('Jan 2, 11am (UTC)', december), dt.timezone.utc).year, 2027)

    def test_codex_multiple_buckets_preserve_window_lengths(self):
        result = usage.parse_codex({'rateLimitsByLimitId': {
            'codex': {'primary': {'usedPercent': 62, 'windowDurationMins': 300, 'resetsAt': 2000000000}},
            'other': {'secondary': {'usedPercent': 9, 'windowDurationMins': 10080, 'resetsAt': 2000000001}}}})
        self.assertEqual([w['used_percent'] for w in result], [62, 9])
        self.assertEqual(result[1]['window_minutes'], 10080)
        self.assertEqual(len(usage.parse_codex({'rateLimits': {'primary': {
            'usedPercent': 0, 'windowDurationMins': 300, 'resetsAt': 2000000000}}})), 1)

    def test_codex_missing_or_malformed_fields_are_not_zero(self):
        for raw in ({}, {'rateLimitsByLimitId': {}}, {'rateLimits': {}},
                    {'rateLimits': {'primary': {'usedPercent': True, 'windowDurationMins': 300, 'resetsAt': 1}}},
                    {'rateLimits': {'primary': {'usedPercent': 0, 'windowDurationMins': 300}}}):
            with self.subTest(raw=raw), self.assertRaises(usage.UsageParseError):
                usage.parse_codex(raw)

    def test_codex_transport_never_starts_a_model_turn(self):
        requests = []
        @contextmanager
        def process(cmd, timeout):
            def ask(messages, matches):
                requests.extend(messages)
                value = {'id': messages[-1]['id'], 'result': {}}
                self.assertTrue(matches(value))
                return value
            yield ask
        with patch.object(think, '_catalog_process', process), patch.object(think, 'resolve_engine'):
            self.assertEqual(think.subscription_usage('codex'), {})
        self.assertEqual([m['method'] for m in requests], ['initialize', 'initialized', 'account/rateLimits/read'])

    def test_startup_menu_cannot_be_mistaken_for_input_prompt(self):
        self.assertTrue(think._usage_prompt_ready('Claude Code v2.1\n❯ Try "edit a file"\n'))
        self.assertTrue(think._usage_prompt_ready('Claude Code v2.1\n❯\u00a0\n'))
        for screen in ('Claude Code v2.1\n❯ 1. Yes, trust this folder\n',
                       'Claude Code v2.1\n❯ 1. Log in\n', 'changed layout'):
            self.assertFalse(think._usage_prompt_ready(screen))

    def test_tmux_cleanup_on_failure_and_environment_isolation(self):
        calls = []
        def run(cmd, **kw):
            calls.append((cmd, kw))
            if 'new-session' in cmd:
                self.assertNotIn('ANTHROPIC_API_KEY', kw['env'])
                self.assertNotIn('OPENAI_API_KEY', kw['env'])
                self.assertNotIn('TMUX', kw['env'])
                raise RuntimeError('startup failed')
            return type('Done', (), {'returncode': 0, 'stdout': ''})()
        with patch.object(think.subprocess, 'run', side_effect=run), \
             patch.object(think.shutil, 'which', return_value='/usr/bin/tmux'), \
             patch.object(think, 'resolve_engine'), \
             patch.dict(think.os.environ, {'ANTHROPIC_API_KEY': 'test', 'OPENAI_API_KEY': 'test', 'TMUX': 'test'}):
            with self.assertRaisesRegex(RuntimeError, 'startup failed'):
                think.subscription_usage('claude')
        self.assertEqual(calls[-1][0][-1], 'kill-server')
        self.assertEqual(calls[0][0][2], calls[-1][0][2])

    def test_tmux_sends_only_builtin_alias_then_cleans_up(self):
        calls = []
        captures = iter(['Claude Code v2.1\n❯\n', SCREEN, SCREEN])
        def run(cmd, **kw):
            calls.append(cmd)
            return type('Done', (), {'returncode': 0,
                        'stdout': next(captures) if 'capture-pane' in cmd else ''})()
        with patch.object(think.subprocess, 'run', side_effect=run), \
             patch.object(think.shutil, 'which', return_value='/usr/bin/tmux'), \
             patch.object(think, 'resolve_engine'), \
             patch.object(think.time, 'monotonic', side_effect=itertools.count()), \
             patch.object(think.time, 'sleep'):
            self.assertEqual(think.subscription_usage('claude'), SCREEN)
        keys = [cmd[-1] for cmd in calls if 'send-keys' in cmd]
        self.assertEqual(keys, ['/cost', 'Enter'])
        self.assertEqual(calls[-1][-1], 'kill-server')


class Frozen(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class StoredUsage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.con = sqlite3.connect(':memory:')
        self.con.row_factory = sqlite3.Row
        self.con.executescript(db.SCHEMA)
        self.addCleanup(self.con.close)
        self.settings = {'usage.thresholds': [80, 95], 'usage.work_threshold': 60,
                         'usage.work_horizon_hours': 24, 'usage.work_lead_hours': 2,
                         'usage.max_age_minutes': 45, 'usage.enabled': True}
        for p in (patch.object(config, 'HOME', pathlib.Path(self.tmp.name)),
                  patch.object(config, 'enabled_engines', return_value=('claude', 'codex')),
                  patch.object(config, 'tz', return_value=ZoneInfo('UTC')),
                  patch.object(config, 'get', side_effect=lambda k, d=None: self.settings.get(k, d)),
                  patch.object(usage.dt, 'datetime', Frozen)):
            p.start(); self.addCleanup(p.stop)

    def snapshot(self, percent=60, reset_hours=5):
        return {'engine': 'claude', 'sampled_at': NOW.timestamp(), 'windows': [{
            'key': 'session', 'label': 'Current session', 'used_percent': percent,
            'window_minutes': 300, 'resets_at': int((NOW + dt.timedelta(hours=reset_hours)).timestamp())}]}

    def obligation(self, hours=4, status='open'):
        self.con.execute("INSERT INTO commitments(created_at,text,kind,due,status) VALUES(?,?,?,?,?)",
                         (NOW.isoformat(), 'Finish work', 'task', (NOW + dt.timedelta(hours=hours)).isoformat(), status))

    def test_failed_parse_keeps_last_reading_and_reports_error(self):
        usage._save(self.con, 'snapshot', 'claude', self.snapshot())
        with patch.object(think, 'subscription_usage', return_value='changed layout'):
            with self.assertRaisesRegex(usage.UsageError, 'Cannot parse'):
                usage.refresh(self.con, 'claude')
        text, failed = usage.report(self.con, ['claude'], cached_only=True)
        self.assertTrue(failed)
        self.assertIn('ERROR: Cannot parse', text)
        self.assertIn('STALE', text)
        self.assertIn('60% used', text)

    def test_successful_reading_clears_error(self):
        usage._save(self.con, 'status', 'claude', {'ok': False, 'error': 'old failure'})
        with patch.object(think, 'subscription_usage', return_value=SCREEN):
            result = usage.refresh(self.con, 'claude')
        self.assertEqual(result['windows'][0]['used_percent'], 12)
        self.assertTrue(usage._saved(self.con, 'status', 'claude')['ok'])

    def test_early_alert_for_work_and_deduplication(self):
        self.obligation()
        with patch.object(usage.notify, 'tell', return_value='telegram') as send:
            self.assertEqual(usage.reminders(self.con, self.snapshot()), 1)
            self.assertEqual(usage.reminders(self.con, self.snapshot(65)), 0)
            self.assertEqual(usage.reminders(self.con, self.snapshot(80)), 1)
            self.assertEqual(usage.reminders(self.con, self.snapshot(96)), 1)
            self.assertIn('You still have Finish work', send.call_args[0][1])

    def test_no_early_alert_if_reset_precedes_preparation_or_work_done(self):
        self.obligation(hours=8)
        self.obligation(hours=4, status='done')
        with patch.object(usage.notify, 'tell') as send:
            self.assertEqual(usage.reminders(self.con, self.snapshot()), 0)
            send.assert_not_called()

    def test_generic_alert_without_work_and_new_reset_window(self):
        with patch.object(usage.notify, 'tell', return_value='telegram'):
            self.assertEqual(usage.reminders(self.con, self.snapshot(80)), 1)
            self.assertEqual(usage.reminders(self.con, self.snapshot(80, 4)), 1)

    def test_stale_or_expired_readings_do_not_alert(self):
        stale = self.snapshot(99); stale['sampled_at'] -= 3600
        with patch.object(usage.notify, 'tell') as send:
            self.assertEqual(usage.reminders(self.con, stale), 0)
            self.assertEqual(usage.reminders(self.con, self.snapshot(99, -1)), 0)
            send.assert_not_called()

    def test_undelivered_alert_can_be_retried(self):
        with patch.object(usage.notify, 'tell', side_effect=['nowhere', 'telegram']):
            self.assertEqual(usage.reminders(self.con, self.snapshot(80)), 0)
            self.assertEqual(usage.reminders(self.con, self.snapshot(80)), 1)

    def test_calendar_dates_do_not_claim_work_is_unfinished(self):
        self.con.execute("INSERT INTO deadlines(source,kind,title,due,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                         ('feed', 'due', 'Assignment', '2026-10-20', NOW.isoformat(), NOW.isoformat()))
        with patch.object(usage.notify, 'tell', return_value='telegram') as send:
            usage.reminders(self.con, self.snapshot(80, 7))
            self.assertIn('Your calendar shows Assignment', send.call_args[0][1])
            self.assertNotIn('unfinished', send.call_args[0][1])
            self.assertNotIn('11:59', send.call_args[0][1])
            self.assertNotIn('23:59', send.call_args[0][1])

    def test_one_provider_failure_does_not_skip_the_other(self):
        with patch.object(usage, 'refresh', side_effect=[usage.UsageError('parse failed'), self.snapshot()]) as refresh, \
             patch.object(usage, 'reminders', return_value=0), patch.object(usage.notify, 'tell'):
            with self.assertRaisesRegex(usage.UsageError, 'claude: parse failed'):
                usage.collect(self.con)
        self.assertEqual([call.args[1] for call in refresh.call_args_list], ['claude', 'codex'])

    def test_cached_report_never_launches_a_cli(self):
        with patch.object(think, 'subscription_usage') as cli:
            text, failed = usage.report(self.con, ['claude'], cached_only=True)
        cli.assert_not_called()
        self.assertTrue(failed)
        self.assertIn('no usage reading', text)


class TelegramUsage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = pathlib.Path(__file__).resolve().parents[1]
        loader = importlib.machinery.SourceFileLoader('tg_usage_test', str(root / 'bin/herald-telegram'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.tg = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.tg)

    def test_private_usage_reads_cache_and_does_not_launch_or_change_session(self):
        msg = {'text': '/usage', 'from': {'id': 42}, 'chat': {'id': -100}, 'message_thread_id': 9}
        state = {'threads': {'-100:9': {'session_id': 'preserved'}}}
        with patch.object(self.tg.config, 'secret', return_value=42), \
             patch.dict(self.tg._private, {'-100:9': {'scratch': 'private'}}), \
             patch.object(self.tg.db, 'session'), \
             patch.object(self.tg.usage, 'report', return_value=('cached usage', False)) as report, \
             patch.object(self.tg, 'send'):
            self.tg._handle_usage({'message': msg}, '-100:9', state)
        self.assertTrue(report.call_args.kwargs['cached_only'])
        self.assertEqual(state['threads']['-100:9']['session_id'], 'preserved')
        self.assertEqual(self.tg.command_word('/usage@bot codex'), '/usage')

    def test_unrecognised_provider_and_unauthorised_user_do_not_sample(self):
        msg = {'text': '/usage unknown', 'from': {'id': 42}, 'chat': {'id': -100}}
        with patch.object(self.tg.config, 'secret', return_value=42), \
             patch.object(self.tg.usage, 'report') as report, patch.object(self.tg, 'send'):
            self.tg._handle_usage({'message': msg}, '-100:0', {})
            msg['from']['id'] = 43
            msg['text'] = '/usage'
            self.tg._handle_usage({'message': msg}, '-100:0', {})
        report.assert_not_called()


if __name__ == '__main__':
    unittest.main()
