"""Sole-provider installs, explicit setup choices and compatible upgrades."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import pathlib
import sqlite3
import sys
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'lib'))
from herald import config, db, think
from setup import engine, home, preflight, providers, services


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Providers(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.home = pathlib.Path(td.name)
        for p in [mock.patch.object(config, 'HOME', self.home),
                  mock.patch.object(config, 'CONFIG_PATH', self.home / 'config.json'),
                  mock.patch.object(config, 'STATE', self.home / 'ledger' / 'state'),
                  mock.patch.object(config, '_cache', None),
                  mock.patch.object(services, 'disable_remote_control', return_value=[])]:
            p.start()
            self.addCleanup(p.stop)
        self.state = engine.State(self.home / 'setup-state.json')

    def choose(self, provider, **answers):
        return providers.apply(self.state, {'providers': provider, **answers})

    def test_fresh_install_never_selects_a_provider_for_you(self):
        self.assertIsNone(config.get('engines.default_engine'))
        self.assertEqual(providers.status(self.state)[0], 'todo')
        self.assertEqual(providers.prompt(self.state).fields[0].default, 'Choose providers')
        self.assertFalse(self.choose('Choose providers').ok)
        with self.assertRaises(ValueError):
            think.resolve_engine(None)
        for provider in config.ENGINE_NAMES:
            self.assertIsNone(config.engine_settings(provider)['model'])
            self.assertIsNone(config.engine_settings(provider)['effort'])
            self.assertIsNone(config.engine_settings(provider)['escalate_model'])

    def test_setup_choices_cover_either_provider_without_requiring_the_other(self):
        for label, provider in [('Claude only', 'claude'), ('Codex only', 'codex')]:
            with self.subTest(provider=provider):
                self.assertTrue(self.choose(label).ok)
                self.assertEqual(config.enabled_engines(), (provider,))
                self.assertEqual(think.resolve_engine(None), provider)
                self.assertEqual(providers.status(self.state)[0], 'done')
                with self.assertRaises(ValueError):
                    think.resolve_engine('codex' if provider == 'claude' else 'claude')
                self.assertFalse(config.remote_control_enabled())

    def test_both_requires_an_explicit_default_and_allows_either(self):
        self.assertFalse(self.choose('Both').ok)
        self.assertFalse(config.CONFIG_PATH.exists())
        for provider in config.ENGINE_NAMES:
            self.assertTrue(self.choose('Both', default_engine=provider).ok)
            self.assertEqual(config.default_engine(), provider)

    def test_remote_control_is_opt_in_and_not_available_on_codex_only(self):
        self.assertFalse(self.choose('Codex only', remote_control=True).ok)
        self.assertTrue(self.choose('Claude only', remote_control=True).ok)
        self.assertTrue(config.remote_control_enabled())
        self.assertTrue(self.choose('Codex only').ok)
        self.assertFalse(config.remote_control_enabled())
        services.disable_remote_control.assert_called_once()

    def test_provider_selection_does_not_skip_creating_the_private_home(self):
        self.choose('Codex only')
        self.assertEqual(home.status(self.state)[0], 'todo')

    def test_codex_only_auth_never_probes_claude_and_refuses_an_api_login(self):
        self.choose('Codex only')
        with mock.patch.object(preflight.shutil, 'which', return_value='/bin/codex'), \
                mock.patch.object(preflight, 'claude_auth') as claude, \
                mock.patch.object(preflight.subprocess, 'run') as run:
            run.return_value = mock.Mock(returncode=0, stdout='', stderr='Logged in using ChatGPT')
            rows = preflight.provider_checks()
            self.assertTrue(all(r['ok'] and r['required'] for r in rows))
            self.assertEqual(run.call_args.args[0], ['codex', 'login', 'status'])
            run.return_value = mock.Mock(returncode=0, stdout='Logged in using an API key: private value', stderr='')
            rows = preflight.provider_checks()
            self.assertFalse(rows[-1]['ok'])
            self.assertNotIn('private value', str(rows))
            claude.assert_not_called()

    def test_claude_only_auth_never_probes_codex_and_requires_subscription(self):
        self.choose('Claude only')
        with mock.patch.object(preflight.shutil, 'which', return_value='/bin/claude'), \
                mock.patch.object(preflight, 'claude_auth') as auth, \
                mock.patch.object(preflight.subprocess, 'run') as run:
            auth.return_value = {'loggedIn': True, 'subscriptionType': 'pro', 'apiProvider': 'firstParty'}
            self.assertTrue(all(r['ok'] for r in preflight.provider_checks()))
            auth.return_value = {'loggedIn': True, 'apiProvider': 'bedrock'}
            self.assertFalse(preflight.provider_checks()[-1]['ok'])
            run.assert_not_called()

    def test_codex_only_preflight_ignores_missing_claude_and_tmux(self):
        self.choose('Codex only')
        def which(cmd, **kw):
            return None if cmd in ('claude', 'tmux', 'gh') else '/bin/' + cmd
        with mock.patch.object(preflight.shutil, 'which', side_effect=which), \
                mock.patch.object(preflight, 'on_wsl', return_value=False), \
                mock.patch.object(preflight.subprocess, 'run', return_value=mock.Mock(
                    returncode=0, stdout='', stderr='Logged in using ChatGPT')):
            required = [r for r in preflight.checks() if r['required']]
            self.assertTrue(all(r['ok'] for r in required), required)
            self.assertFalse(any('Claude' in r['name'] for r in required))

    def test_optional_surface_not_scheduled_on_any_platform_for_codex_only(self):
        self.choose('Codex only')
        for platform in ('systemd', 'launchd', 'container'):
            with mock.patch.object(services, 'platform_name', return_value=platform):
                units = services.wanted_units(True)
                self.assertFalse(any('brain' in u for u in units), units)
                self.assertTrue(any('telegram' in u for u in units))
        self.assertNotIn('com.herald.brain', services.launchd_jobs())
        self.choose('Claude only', remote_control=True)
        with mock.patch.object(services, 'platform_name', return_value='systemd'):
            self.assertIn('herald-brain.service', services.wanted_units(False))

    def test_legacy_settings_still_merge_and_writes_use_the_new_name(self):
        config.CONFIG_PATH.write_text(json.dumps({'engines': {'primary': {'model': 'legacy', 'effort': 'low'},
                                                               'claude': {'model': 'chosen'}}}))
        config.reload()
        self.assertEqual(config.get('engines.primary.model'), 'chosen')
        self.assertEqual(config.get('engines.claude.effort'), 'low')
        config.set_user('engines.primary.model', 'updated')
        self.assertEqual(config.get('engines.claude.model'), 'updated')

    def test_terminal_launches_either_cli_with_its_own_settings(self):
        for provider in config.ENGINE_NAMES:
            self.choose('Claude only' if provider == 'claude' else 'Codex only')
            with mock.patch.object(think.subprocess, 'call', return_value=0) as call:
                self.assertEqual(think.interactive(), 0)
            args = call.call_args.args[0]
            self.assertEqual(args[0], provider)
            self.assertNotIn('--model', args)
            if provider == 'codex':
                self.assertIn('project_doc_fallback_filenames=["CLAUDE.md"]', args)
            self.assertNotIn('OPENAI_API_KEY', call.call_args.kwargs['env'])

    def test_claude_native_default_omits_the_model_flag(self):
        with mock.patch.object(think, '_run_process', return_value=(0, None, None, '', '', False)) as run:
            think._run_claude('probe', model=None, effort=None, cwd=ROOT,
                             timeout=1, idle_timeout=1, resume=None, allowed_tools=None,
                             append_system_prompt=None, json_schema=None, permission_mode='plan', add_dirs=None)
        self.assertNotIn('--model', run.call_args.args[1])

    def test_telegram_catalog_only_asks_enabled_provider(self):
        tg = load(ROOT / 'bin' / 'herald-telegram', 'tg_providers_catalog')
        for provider in config.ENGINE_NAMES:
            self.choose('Claude only' if provider == 'claude' else 'Codex only')
            with mock.patch.object(think, 'available_models', return_value=[]) as catalog:
                rows, errors = tg._model_catalog()
            self.assertEqual(errors, [])
            catalog.assert_called_once_with(provider)

    def test_telegram_new_topics_use_the_user_default_for_either_provider(self):
        tg = load(ROOT / 'bin' / 'herald-telegram', 'tg_providers_default')
        from contextlib import nullcontext
        from types import SimpleNamespace
        msg = {'chat': {'id': 7, 'type': 'private'}, 'from': {'id': 42},
               'message_id': 1, 'text': 'hello'}
        working = mock.MagicMock()
        working.__enter__.return_value.elapsed = 0
        for provider in config.ENGINE_NAMES:
            self.choose('Claude only' if provider == 'claude' else 'Codex only')
            with mock.patch.object(tg.config, 'secret', return_value=42), \
                    mock.patch.object(tg, 'send', return_value=[]), \
                    mock.patch.object(tg, 'delta_for', return_value=''), \
                    mock.patch.object(tg, 'orientation_for', return_value='card'), \
                    mock.patch.object(tg, '_reply_target', return_value=None), \
                    mock.patch.object(tg, '_remember_turn'), \
                    mock.patch.object(tg.activity, 'Turn', return_value=nullcontext()), \
                    mock.patch.object(tg, 'Working', return_value=working), \
                    mock.patch.object(tg.think, 'think', return_value=SimpleNamespace(
                        ok=True, cancelled=False, text='answer', session_id='session', cost_usd=None)) as call, \
                    mock.patch('builtins.print'):
                state = {'threads': {}}
                tg.handle({'message': msg}, state)
                self.assertEqual(call.call_args.kwargs['engine'], provider)
                self.assertIsNone(call.call_args.kwargs['model'])
                self.assertEqual(state['threads']['7:main']['engine'], provider)
                # A disabled provider command must leave the pointer intact.
                msg['text'] = '/codex' if provider == 'claude' else '/claude'
                tg.handle({'message': msg}, state)
                self.assertEqual(state['threads']['7:main']['engine'], provider)
                self.assertEqual(call.call_count, 1)
                msg['text'] = 'hello'

    def test_unattended_installer_never_installs_an_unselected_cli(self):
        # Run the real choice function with installation stubbed. No network,
        # package manager, login, or program install is involved.
        script = (ROOT / 'install.sh').read_text().split('\nmain() {')[0]
        script += "\ninstall_claude() { echo selected-claude; }\ninstall_codex() { echo selected-codex; }\nchoose_providers\n"
        for choice, expected in [('', []), ('claude', ['claude']), ('codex', ['codex']),
                                 ('both', ['claude', 'codex'])]:
            config.CONFIG_PATH.unlink(missing_ok=True)
            env = config.agent_env() | {'HERALD_CONTAINER': '1', 'HERALD_HOME': str(self.home),
                                        'HERALD_ENGINES': choice}
            result = subprocess.run(['bash'], input=script, text=True, capture_output=True, cwd=ROOT, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ['selected-' + p for p in expected])
            if expected:
                saved = json.loads(config.CONFIG_PATH.read_text())['engines']
                self.assertEqual(saved['enabled'], expected)
                self.assertEqual(saved['default_engine'], expected[0] if len(expected) == 1 else None)
            else:
                self.assertFalse(config.CONFIG_PATH.exists())

    def test_upgrade_freezes_only_historical_defaults_not_new_install_defaults(self):
        migration = load(ROOT / 'migrations' / '0003_provider_choices.py', 'provider_old_default')
        config.CONFIG_PATH.write_text('{}')
        con = sqlite3.connect(':memory:')
        self.addCleanup(con.close)
        con.executescript(db.SCHEMA)
        migration.apply(con)
        self.assertEqual(config.default_engine(), 'claude')
        self.assertEqual(config.get('engines.claude.model'), 'sonnet')
        self.assertEqual(config.get('engines.claude.escalate_model'), 'opus')
        # That choice is now stored in the person's config, never shipped.
        config.CONFIG_PATH.unlink()
        config.reload()
        with self.assertRaises(ValueError):
            config.default_engine()


    def test_upgrade_preserves_choices_and_is_idempotent(self):
        migration = load(ROOT / 'migrations' / '0003_provider_choices.py', 'provider_migration')
        config.CONFIG_PATH.write_text(json.dumps({'engines': {'default_engine': 'codex',
                                  'primary': {'model': 'old-model', 'effort': 'low'}}}))
        config.reload()
        con = sqlite3.connect(':memory:')
        self.addCleanup(con.close)
        con.executescript(db.SCHEMA)
        state = {'threads': {'old': {'session_id': 'one', 'engine': None},
                             'codex': {'engine': 'codex', 'model': None, 'session_id': 'two'}}}
        con.execute("INSERT INTO collector_state (collector,cursor) VALUES ('telegram',?)", (json.dumps(state),))
        migration.apply(con)
        self.assertEqual(config.default_engine(), 'codex')
        self.assertEqual(config.get('engines.claude.model'), 'old-model')
        self.assertEqual(config.get('engines.primary.effort'), 'low')
        migrated = json.loads(con.execute("SELECT cursor FROM collector_state WHERE collector='telegram'").fetchone()[0])
        self.assertEqual(migrated['threads']['old']['model'], 'opus')
        self.assertEqual(migrated['threads']['old']['session_id'], 'one')
        self.assertIsNone(migrated['threads']['codex']['model'])
        before = config.CONFIG_PATH.read_text()
        migration.apply(con)
        self.assertEqual(config.CONFIG_PATH.read_text(), before)


if __name__ == '__main__':
    unittest.main()
