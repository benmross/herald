"""What settings replies say about the model, thinking level and what carries over."""
import importlib.machinery
import importlib.util
import pathlib
import sys
import time
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
from herald import config, topicstatus

CONFIG = {'engines.default_engine': 'claude', 'engines.claude.model': 'opus',
          'engines.claude.effort': 'high', 'engines.codex.model': 'gpt-6.1-sol',
          'engines.codex.effort': 'medium', 'engines.renewal_context_share': 0.75}
real_get = config.get


def fake_get(key, default=None):
    if key in CONFIG:
        return CONFIG[key]
    if key.startswith('engines.') and key.endswith(('.model', '.effort')):
        return default
    return real_get(key, default)


class Names(unittest.TestCase):
    def test_model_names_read_as_people_say_them(self):
        cases = {('claude', 'claude-opus-5-5'): 'Claude Opus 5.5',
                 ('claude', 'opus'): 'Claude Opus',
                 ('codex', 'gpt-6.1-sol'): 'GPT-6.1 Sol',
                 ('kiro', 'claude-opus-5.5'): 'Kiro (Claude Opus 5.5)',
                 ('codex', 'o9'): 'Codex o9',
                 ('claude', None): "Claude's default model"}
        for (engine, model), want in cases.items():
            self.assertEqual(topicstatus.model_name(engine, model), want)


class Wording(unittest.TestCase):
    def setUp(self):
        p = patch.object(config, 'get', side_effect=fake_get)
        p.start(); self.addCleanup(p.stop)
        p = patch.object(config, 'default_engine', return_value='claude')
        p.start(); self.addCleanup(p.stop)

    def test_status_uses_configured_defaults_not_cli_jargon(self):
        self.assertEqual(topicstatus.now({}), 'Model: Claude Opus · Thinking: Default (high)')
        self.assertEqual(topicstatus.now({'engine': 'codex', 'effort': 'auto'}),
                         'Model: GPT-6.1 Sol · Thinking: Default (Codex decides)')

    def test_nothing_mentions_the_ledger(self):
        ts = {'engine': 'codex'}
        for text in (topicstatus.new_note(ts, True), topicstatus.switch_note(ts, 'claude', True),
                     topicstatus.same_conversation_note(ts)):
            self.assertNotIn('ledger', text)

    def test_switch_with_nothing_to_carry_does_not_mention_a_transcript(self):
        text = topicstatus.switch_note({'engine': 'codex'}, 'claude', False)
        self.assertIn('Your next message starts the conversation.', text)
        self.assertNotIn('summary', text)

    def test_context_summary_explains_memory_and_commands(self):
        ts = {'session_id': 's', 'turns': 3, 'context_tokens': 41_000,
              'context_window': 100_000, 'work': {'request': 'fix the website'}}
        text = topicstatus.context_summary(ts, None, now_ts=time.time())
        self.assertIn('Model: Claude Opus · Thinking: Default (high)', text)
        self.assertIn('41,000 of 100,000 tokens (41%)', text)
        self.assertIn('At 75,000 (75%)', text)
        self.assertIn('Working on: fix the website', text)
        self.assertNotIn('/new', text)

    def test_context_summary_reads_turns_cost_and_last_turn(self):
        import sqlite3
        con = sqlite3.connect(':memory:'); con.row_factory = sqlite3.Row
        con.execute('CREATE TABLE runs (id INTEGER PRIMARY KEY, session_id, ts, duration_ms, cost_usd, '
                    'input_tokens, output_tokens, cache_read, context_tokens, round_trips)')
        con.execute("INSERT INTO runs VALUES (1,'s','2026-11-16T10:00:00',5000,0.5,100,20,9000,9120,3)")
        con.execute("INSERT INTO runs VALUES (2,'s','2026-11-16T10:05:00',45200,1.25,1200,300,88000,89500,8)")
        text = topicstatus.context_summary({'session_id': 's'}, con, now_ts=time.time())
        self.assertIn('2 messages', text)
        self.assertIn('$1.75 so far', text)
        self.assertIn('Context: 89,500 tokens.', text)
        self.assertIn('Last message: 45.2s, 8 steps; 1,200 new tokens in, 88,000 reread from cache, 300 out', text)

    def test_explicit_auto_names_the_engines_own_default_when_known(self):
        ts = {'engine': 'codex', 'model': 'm', 'effort': 'auto',
              'effort_caps': {'engine': 'codex', 'model': 'm', 'efforts': ['low'], 'default_effort': 'low'}}
        self.assertEqual(topicstatus.thinking(ts), 'Default (low)')


class Guide(unittest.TestCase):
    def test_lists_only_enabled_providers(self):
        with patch.object(config, 'enabled_engines', return_value=('claude',)):
            self.assertNotIn('/codex', topicstatus.guide())
        with patch.object(config, 'enabled_engines', return_value=('claude', 'codex')):
            self.assertIn('/claude, /codex  Switch provider', topicstatus.guide())


class Bridge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loader = importlib.machinery.SourceFileLoader('tg_status_test', str(ROOT / 'bin/herald-telegram'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.tg = importlib.util.module_from_spec(spec); loader.exec_module(cls.tg)

    def setUp(self):
        self.key = '-100:9'
        self.state = {'offset': 0, 'threads': {self.key: {
            'engine': 'claude', 'session_id': 'kept', 'turns': 4, 'last_ts': time.time(),
            'effort': 'max', 'work': {'request': 'build it'}}}}
        self.sent = []
        self.msg = {'chat': {'id': -100}, 'message_thread_id': 9, 'message_id': 5, 'from': {'id': 42}}
        for p in (patch.object(self.tg.config, 'secret', return_value=42),
                  patch.object(self.tg.config, 'get', side_effect=fake_get),
                  patch.object(self.tg.config, 'default_engine', return_value='claude'),
                  patch.object(self.tg.config, 'reload'),
                  patch.object(self.tg.think, 'resolve_engine', side_effect=lambda e: e or 'claude'),
                  patch.object(self.tg, '_remember_turn'),
                  patch.object(self.tg, '_sync_topic_name'),
                  patch.object(self.tg, 'api', return_value={'ok': True, 'result': {'message_id': 77}}),
                  patch.object(self.tg, 'send', side_effect=lambda c, t, *a, **k: self.sent.append(t) or [78])):
            p.start(); self.addCleanup(p.stop)

    def say(self, text):
        self.tg.handle({'message': self.msg | {'text': text}}, self.state)
        return self.sent[-1]

    def test_new_names_what_is_forgotten_and_what_is_running(self):
        text = self.say('/clear')
        self.assertIn('forgotten', text)
        self.assertIn('Model: Claude Opus · Thinking: max', text)

    def test_new_says_it_keeps_the_last_messages(self):
        self.assertIn('last few messages',
                      topicstatus.new_note({}, True, kept=True))
        self.assertIn('nothing here to forget', topicstatus.new_note({}, False, kept=True))

    def test_clear_then_switch_says_nothing_carries(self):
        self.say('/clear')
        text = self.say('/codex')
        self.assertIn('Switched to Codex. Your next message starts the conversation.', text)
        self.assertIn('Model: GPT-6.1 Sol · Thinking: Default (medium)', text)
        self.assertIn('earlier choice of max', text)

    def test_switch_mid_conversation_carries_a_summary(self):
        text = self.say('/codex')
        self.assertIn("Codex can't read Claude's", text)
        self.assertIn('summary', text)
        self.assertIsNone(self.state['threads'][self.key]['effort'])

    def test_same_engine_model_keeps_conversation_and_level(self):
        text = self.say('/sonnet')
        self.assertIn('Same conversation', text)
        self.assertIn('Claude Sonnet · Thinking: max', text)
        self.assertEqual(self.state['threads'][self.key]['session_id'], 'kept')

    def test_help_and_start_send_the_guide_without_a_turn(self):
        for word in ('/help', '/start', '/help@somebot'):
            self.sent.clear()
            with patch.object(self.tg.think, 'think', side_effect=AssertionError('no model turn')):
                self.tg._handle_help({'message': self.msg | {'text': word}})
            self.assertIn('/new  Start a new conversation', self.sent[-1])
        self.assertEqual(self.tg.command_word('/help'), '/help')
        self.assertIn({'command': 'help', 'description': 'How to use these commands'},
                      self.tg.command_menu('short', {}))

    def test_effort_reply_names_the_model(self):
        text = self.say('/low')
        self.assertIn('Thinking set to low. Same conversation', text)
        self.assertIn('Model: Claude Opus · Thinking: low', text)


if __name__ == '__main__':
    unittest.main()
