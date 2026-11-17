"""Crash boundaries: durable requests, native sessions, steering and delivery."""
import contextlib
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
from herald import db, tgturns, think, tgsessions


def bridge():
    loader = importlib.machinery.SourceFileLoader('tg_recovery_test', str(ROOT / 'bin/herald-telegram'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def update(number=1, text='Finish the report', topic=7):
    return {'update_id': number, 'message': {'message_id': number + 10,
        'from': {'id': 123}, 'chat': {'id': -42, 'type': 'supergroup'},
        'message_thread_id': topic, 'date': int(time.time()), 'text': text}}


class Recovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / 'test.db'
        with contextlib.closing(sqlite3.connect(self.path)) as con:
            con.executescript(db.SCHEMA)
        @contextlib.contextmanager
        def session():
            con = sqlite3.connect(self.path, timeout=10)
            con.row_factory = sqlite3.Row
            try:
                with con:
                    yield con
            finally:
                con.close()
        self.session = session
        patches = [mock.patch.object(db, 'session', session),
            mock.patch('herald.config.secret', side_effect=lambda key, *a: 123 if key == 'telegram.user_id' else 'test'),
            mock.patch('herald.config.reload'), mock.patch('herald.config.LOGS', pathlib.Path(self.tmp.name))]
        for patch in patches:
            patch.start(); self.addCleanup(patch.stop)
        self.tg = bridge()
        self.api = mock.patch.object(self.tg, 'api', return_value={'ok': True, 'result': {'message_id': 90}})
        self.api.start(); self.addCleanup(self.api.stop)
        for name in ['_sync_topic_name', '_observe_topic_name']:
            p = mock.patch.object(self.tg, name); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(self.tg, 'delta_for', return_value=''); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(self.tg, 'session_input', return_value=('Continue report', None)); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(self.tg.think, 'resolve_engine', return_value='claude'); p.start(); self.addCleanup(p.stop)
        self.state = {'threads': {}, 'offset': 0}
        self.calls = []

    def start(self, u=None, session_id='native-original'):
        u = u or update()
        self.assertTrue(self.tg._journal_receive(u))
        ts = {'engine': 'claude', 'model': 'test-model', 'effort': 'high', 'turns': 0, 'last_ts': 0}
        root = tgturns.begin(u, '-42:7', ts)
        if session_id:
            tgturns.session(root, session_id, ts, -42, [u['message']['message_id']])
        return root, u

    def engine(self, prompt, **kw):
        self.calls.append(dict(kw, prompt=prompt))
        kw['on_progress'](think.Progress('session', kw.get('resume') or 'native-new'))
        return think.Result(ok=True, engine='claude', model='test-model', text='The report is ready.',
                            session_id=kw.get('resume') or 'native-new')

    def recover(self):
        with mock.patch.object(think, 'think', side_effect=self.engine):
            self.tg._recover_turns(self.state)
            for key in list(self.state['threads']):
                lock = self.tg._lock_for(key)
                self.assertTrue(lock.acquire(timeout=5), 'recovery worker did not finish')
                lock.release()

    def test_fresh_native_id_is_saved_before_any_model_tools(self):
        u = update(); self.tg._journal_receive(u)
        def engine(prompt, **kw):
            kw['on_progress'](think.Progress('session', 'early-id'))
            row = tgturns.get(u['_turn_ids'][0])
            self.assertEqual(row['session_id'], 'early-id')
            self.assertEqual(self.state['threads']['-42:7']['session_id'], 'early-id')
            with self.session() as con:
                self.assertEqual(tgsessions.lookup(con, -42, 11)['session_id'], 'early-id')
            return think.Result(ok=True, engine='claude', text='Done', session_id='early-id')
        with mock.patch.object(think, 'think', side_effect=engine):
            self.tg.handle(u, self.state)
        self.assertEqual(tgturns.get(u['_turn_ids'][0])['status'], 'done')

    def test_interrupted_turn_resumes_exact_native_session_and_model(self):
        root, u = self.start()
        self.recover()
        self.assertEqual(self.calls[0]['resume'], 'native-original')
        self.assertEqual(self.calls[0]['model'], 'test-model')
        self.assertEqual(self.calls[0]['effort'], 'high')
        self.assertEqual(tgturns.get(root)['status'], 'done')
        self.assertFalse(self.tg._journal_receive(update()))

    def test_crash_before_native_init_restarts_original_request(self):
        root, u = self.start(session_id=None)
        self.recover()
        self.assertIsNone(self.calls[0]['resume'])
        self.assertEqual(tgturns.get(root)['status'], 'done')

    def test_answer_saved_before_crash_is_delivered_without_model(self):
        root, u = self.start()
        tgturns.answer(root, {'ok': True, 'text': 'Already finished', 'session_id': 'native-original'}, {})
        self.recover()
        self.assertEqual(self.calls, [])
        self.assertEqual(tgturns.get(root)['status'], 'done')

    def test_completion_and_all_reply_chunks_commit_before_network(self):
        root, u = self.start()
        def api(method, **kw):
            self.assertEqual(tgturns.get(root)['status'], 'done')
            with self.session() as con:
                self.assertGreaterEqual(con.execute('select count(*) from telegram_outbox').fetchone()[0], 1)
            return {'ok': True, 'result': {'message_id': 99}}
        with mock.patch.object(self.tg, 'api', side_effect=api):
            self.tg.send(-42, 'x' * 4500, 7, durable=True, turn_root=root, session_id='native-original')
        self.assertEqual(tgturns.get(root)['status'], 'done')
        self.recover(); self.assertEqual(self.calls, [])

    def test_queue_failure_does_not_consume_the_request(self):
        root, u = self.start()
        with mock.patch.object(self.tg.tgoutbox, 'enqueue_in', side_effect=sqlite3.OperationalError('locked')):
            with self.assertRaises(sqlite3.OperationalError):
                self.tg.send(-42, 'Done', 7, durable=True, turn_root=root)
        self.assertEqual(tgturns.get(root)['status'], 'running')

    def test_steering_survives_repeated_recovery(self):
        root, u = self.start()
        child = update(2, 'Use the new numbers'); self.tg._journal_receive(child)
        self.assertTrue(tgturns.attach(root, child['_turn_ids']))
        recovered = tgturns.recovery_update(tgturns.get(root))
        self.assertEqual(recovered['_recovery_followups'][0]['message']['text'], 'Use the new numbers')
        tgturns.begin(recovered, '-42:7', {})
        again = tgturns.recovery_update(tgturns.get(root))
        self.assertEqual(len(again['_recovery_followups']), 1)
        with self.session() as con: tgturns.finish_in(con, root)
        self.assertEqual(tgturns.get(child['_turn_ids'][0])['status'], 'done')

    def test_failed_auto_recovery_keeps_session_for_next_user_message(self):
        root, u = self.start()
        with self.session() as con: con.execute('UPDATE telegram_turns SET attempts=3 WHERE id=?', (root,))
        self.recover()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.state['threads']['-42:7']['session_id'], 'native-original')
        self.assertTrue(self.state['threads']['-42:7']['_crash_resume'])
        self.assertEqual(tgturns.get(root)['status'], 'paused')

    def test_private_command_fences_same_poll_before_worker_handles_it(self):
        self.tg._journal_receive(update(1, '/private'))
        self.tg._journal_receive(update(2, 'Private information'))
        with self.session() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM telegram_turns').fetchone()[0], 0)
        self.tg._journal_receive(update(3, '/private off'))
        self.tg._journal_receive(update(4, 'Ordinary message'))
        with self.session() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM telegram_turns').fetchone()[0], 1)

    def test_stop_prevents_startup_recovery(self):
        root, u = self.start()
        with mock.patch.object(think, 'cancel', return_value=False):
            self.tg._handle_stop(update(2, '/stop'), '-42:7', self.state)
        self.recover(); self.assertEqual(self.calls, [])

    def test_resume_is_scoped_to_topic_and_starts_no_model(self):
        self.start()
        self.tg.handle(update(2, '/resume native-original'), self.state)
        self.assertEqual(self.state['threads']['-42:7']['session_id'], 'native-original')
        self.tg.handle(update(3, '/resume native-original', topic=9), self.state)
        self.assertIsNone(self.state['threads']['-42:9'].get('session_id'))

    def test_recovery_marks_only_the_latest_progress_message(self):
        root, u = self.start()
        tgturns.progress(root, -42, [100, 101])
        self.recover()
        with self.session() as con:
            edited = [json.loads(r[0])['message_id'] for r in con.execute("SELECT params FROM telegram_outbox WHERE method='editMessageText'")]
        self.assertEqual(edited, [101])

    def test_uninjected_followup_is_included_before_interrupted_work_resumes(self):
        root, u = self.start()
        self.tg._journal_receive(update(2, 'Do not publish; leave a draft'))
        with mock.patch.object(self.tg, 'session_input', side_effect=lambda lead, body, ts, fresh: (body, None)):
            self.recover()
        self.assertEqual(len(self.calls), 1)
        self.assertIn('Do not publish; leave a draft', self.calls[0]['prompt'])

    def test_resume_checkpoint_survives_crash_before_engine_initialization(self):
        u = update(); self.tg._journal_receive(u)
        root = tgturns.begin(u, '-42:7', {'engine': 'claude', 'session_id': 'prior-session', 'model': 'test-model'})
        self.recover()
        self.assertEqual(self.calls[0]['resume'], 'prior-session')

    def test_album_received_before_crash_restarts_as_one_turn(self):
        for number in (1, 2):
            u = update(number, '')
            u['message'].update(media_group_id='album', document={'file_id': str(number)})
            self.tg._journal_receive(u)
        tgturns.coalesce_queued()
        rows = tgturns.unfinished()
        self.assertEqual(len(rows), 1)
        rebuilt = tgturns.recovery_update(rows[0])
        self.assertEqual(len(rebuilt['_album']), 2)
        self.assertEqual(len(rebuilt['_turn_ids']), 2)
        self.assertEqual(rebuilt['_recovery_followups'], [])

    def test_split_paste_received_before_crash_restarts_whole(self):
        self.tg._journal_receive(update(1, 'A' * 4000))
        self.tg._journal_receive(update(2, 'the ending'))
        tgturns.coalesce_queued()
        rows = tgturns.unfinished()
        self.assertEqual(len(rows), 1)
        self.assertTrue(tgturns.recovery_update(rows[0])['message']['text'].endswith('\nthe ending'))

    def test_cancelled_queued_request_does_not_start_after_topic_unlocks(self):
        u = update(); self.tg._journal_receive(u)
        tgturns.cancel('-42:7')
        with mock.patch.object(think, 'think') as run:
            self.tg.handle(u, self.state)
        run.assert_not_called()

    def test_additive_migration_is_idempotent_and_preserves_existing_sessions(self):
        spec = importlib.util.spec_from_file_location('recovery_migration', ROOT / 'migrations/0013_telegram_turn_recovery.py')
        migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
        self.start()
        with self.session() as con:
            migration.apply(con); migration.apply(con)
            self.assertEqual(tgsessions.lookup(con, -42, 11)['session_id'], 'native-original')

    def test_worker_isolation_is_bounded_and_tied_to_bridge_lifetime(self):
        with mock.patch.dict(os.environ, {'HERALD_WORKER_PARENT': 'herald-telegram.service'}), \
                mock.patch.object(think.shutil, 'which', side_effect=lambda c: '/usr/bin/' + c), \
                mock.patch.object(think, '_scope_oom_policy', return_value=['--property=OOMPolicy=kill']), \
                mock.patch.object(think.subprocess, 'Popen') as spawn:
            think._spawn('claude', ['claude', '-p'], cwd=str(ROOT))
        command = spawn.call_args.args[0]
        self.assertIn('--slice=herald-work.slice', command)
        self.assertIn('--property=BindsTo=herald-telegram.service', command)
        self.assertIn('--property=MemoryMax=3G', command)
        self.assertIn('--property=OOMPolicy=kill', command)
        self.assertIn('500', command)
        self.assertEqual(command[-2:], ['claude', '-p'])

    @unittest.skipUnless(sys.platform == 'linux', 'SIGKILL boundary')
    def test_real_sigkill_leaves_checkpoint_recoverable(self):
        # A new OS process commits the request and native ID, then dies without
        # running finally blocks. Recovery sees only what survived on disk.
        code = '''import contextlib,sqlite3,json,os,signal,sys
from herald import db,tgturns
@contextlib.contextmanager
def session():
 c=sqlite3.connect(sys.argv[1]);c.row_factory=sqlite3.Row
 try:
  with c: yield c
 finally:c.close()
db.session=session
u=json.loads(sys.argv[2]);i=tgturns.receive(u,'-42:7');u['_turn_ids']=[i]
r=tgturns.begin(u,'-42:7',{'engine':'claude','model':'test-model'})
tgturns.session(r,'killed-process-session',{'engine':'claude','model':'test-model'},-42,[11])
os.kill(os.getpid(),signal.SIGKILL)
'''
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'lib'))
        r = subprocess.run([sys.executable, '-c', code, str(self.path), json.dumps(update())], env=env)
        self.assertEqual(r.returncode, -signal.SIGKILL)
        self.recover()
        self.assertEqual(self.calls[0]['resume'], 'killed-process-session')


if __name__ == '__main__': unittest.main()
