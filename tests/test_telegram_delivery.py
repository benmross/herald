"""Offline recovery of completed replies during Telegram outages."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import pathlib
import sqlite3
import sys
import unittest
import urllib.error
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
from herald import db, tgapi, tgoutbox, tgsessions


def bridge():
    loader = importlib.machinery.SourceFileLoader('tg_delivery_test', str(ROOT / 'bin/herald-telegram'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def http_error(code, payload=None):
    return urllib.error.HTTPError('https://example.invalid', code, 'error', {},
                                  io.BytesIO(json.dumps(payload or {}).encode()))


class Transport(unittest.TestCase):
    def setUp(self):
        self.client = tgapi.Client()
        self.clock = 100.0
        def sleep(seconds):
            self.clock += seconds
        for patch in (
            mock.patch.object(tgapi.config, 'secret', return_value='fake-test-token'),
            mock.patch.object(tgapi.time, 'monotonic', lambda: self.clock),
            mock.patch.object(tgapi.time, 'sleep', side_effect=sleep),
            mock.patch.object(tgapi.urllib.request, 'urlopen'),
            mock.patch('sys.stderr', io.StringIO()),
        ):
            value = patch.start()
            self.addCleanup(patch.stop)
            if patch.attribute == 'urlopen':
                self.request = value

    def response(self, value):
        result = mock.MagicMock()
        result.__enter__.return_value = io.BytesIO(json.dumps(value).encode())
        return result

    def test_gateway_failures_back_off_then_deliver_once_confirmed(self):
        self.request.side_effect = [http_error(502), http_error(503), self.response({'ok': True, 'result': {'message_id': 7}})]
        result = self.client.call('sendMessage', chat_id=1, text='Ready')
        self.assertTrue(result['ok'])
        self.assertEqual(self.clock, 106)
        self.assertEqual(self.request.call_count, 3)

    def test_retry_after_is_shared_and_typing_does_not_send_during_cooldown(self):
        self.request.side_effect = http_error(429, {'parameters': {'retry_after': 60}})
        result = self.client.call('sendMessage', chat_id=1, text='Ready')
        self.assertEqual(result['error_code'], 429)
        self.assertEqual(self.request.call_count, 1)
        self.client.call('sendChatAction', chat_id=1, action='typing')
        self.client.call('getUpdates', offset=10)
        self.assertEqual(self.request.call_count, 1)
        self.clock += 60
        self.request.side_effect = None
        self.request.return_value = self.response({'ok': True, 'result': []})
        self.assertTrue(self.client.call('getUpdates', offset=10)['ok'])

    def test_short_flood_control_delay_is_respected_before_retry(self):
        self.request.side_effect = [http_error(429, {'parameters': {'retry_after': 3}}), self.response({'ok': True})]
        self.assertTrue(self.client.call('sendMessage', chat_id=1, text='Ready')['ok'])
        self.assertEqual(self.clock, 103)

    def test_permanent_rejections_are_not_retried(self):
        self.request.side_effect = http_error(403, {'description': 'Forbidden'})
        self.assertEqual(self.client.call('sendMessage', chat_id=1, text='Ready')['error_code'], 403)
        self.assertEqual(self.request.call_count, 1)

    def test_already_applied_terminal_edit_is_success(self):
        self.request.side_effect = http_error(400, {'description': 'Bad Request: message is not modified'})
        self.assertTrue(self.client.call('editMessageText', chat_id=1, message_id=4, text='done')['ok'])
        self.assertEqual(self.request.call_count, 1)

    def test_network_errors_never_log_the_token_or_request_url(self):
        self.request.side_effect = urllib.error.URLError('https://api.telegram.org/botfake-test-token/sendMessage')
        self.client.call('sendMessage', chat_id=1, text='Ready')
        self.assertNotIn('fake-test-token', sys.stderr.getvalue())
        self.assertNotIn('https://', sys.stderr.getvalue())


class Delivery(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(':memory:')
        self.con.row_factory = sqlite3.Row
        self.con.executescript(db.SCHEMA)
        self.addCleanup(self.con.close)
        @contextlib.contextmanager
        def session():
            with self.con:
                yield self.con
        patch = mock.patch.object(db, 'session', session)
        patch.start()
        self.addCleanup(patch.stop)
        self.tg = bridge()
        self.addCleanup(self.tg._private.clear)
        self.accept = mock.Mock(return_value={'ok': True, 'result': {'message_id': 50}})

    def rows(self):
        return self.con.execute('SELECT * FROM telegram_outbox ORDER BY id').fetchall()

    def ready(self):
        self.con.execute('UPDATE telegram_outbox SET next_attempt=0')
        self.con.commit()

    def test_outage_survives_bridge_reload_without_repeating_successful_chunks(self):
        responses = [{'ok': True, 'result': {'message_id': 10}}, {'ok': False, 'error_code': 502}]
        with mock.patch.object(self.tg, 'api', side_effect=responses) as api:
            ids = self.tg.send(-100, 'first ' * 1000, 5, durable=True, session_id='original-session')
        self.assertEqual(ids, [10])
        self.assertEqual(api.call_count, 2)  # No immediate plain-text resend on 502.
        remaining = self.rows()
        self.assertEqual(len(remaining), 1)
        queued_text = json.loads(remaining[0]['params'])['text']
        self.ready()
        reloaded = bridge()
        with mock.patch.object(reloaded, 'api', self.accept):
            reloaded.tgoutbox.retry(reloaded.api)
        self.assertEqual(self.rows(), [])
        self.accept.assert_called_once()
        self.assertEqual(self.accept.call_args.kwargs['text'], queued_text)
        self.assertEqual(self.con.execute('SELECT session_id FROM telegram_messages WHERE message_id=50').fetchone()[0], 'original-session')

    def test_failed_first_chunk_holds_later_chunks_until_it_lands(self):
        with mock.patch.object(self.tg, 'api', return_value={'ok': False, 'error_code': 502}) as api:
            self.tg.send(-100, 'chunk ' * 1000, 5, durable=True)
        self.assertEqual(api.call_count, 1)
        expected = [json.loads(row['params'])['text'] for row in self.rows()]
        self.ready()
        tgoutbox.retry(self.accept)
        self.assertEqual([c.kwargs['text'] for c in self.accept.call_args_list], expected)
        self.assertEqual(self.rows(), [])

    def test_formatting_error_alone_triggers_plain_text_fallback(self):
        self.accept.side_effect = [{'ok': False, 'error_code': 400, 'description': "Can't parse entities"}, {'ok': True, 'result': {'message_id': 50}}]
        with mock.patch.object(self.tg, 'api', self.accept):
            self.tg.send(1, '**hello**', durable=True)
        self.assertIn('parse_mode', self.accept.call_args_list[0].kwargs)
        self.assertNotIn('parse_mode', self.accept.call_args_list[1].kwargs)
        self.assertEqual(self.accept.call_args_list[1].kwargs['text'], '**hello**')
        self.assertEqual(self.rows(), [])

    def test_permanent_rejection_is_retained_but_not_retried(self):
        with mock.patch.object(self.tg, 'api', return_value={'ok': False, 'error_code': 403}), contextlib.redirect_stderr(io.StringIO()):
            self.tg.send(1, 'reply', durable=True)
        self.assertEqual(self.rows()[0]['state'], 'failed')
        tgoutbox.retry(self.accept)
        self.accept.assert_not_called()

    def test_finished_progress_recovers_and_cannot_revert_to_working(self):
        work = self.tg.Working(1)
        work._msg_id = 20
        work._lines = ['Read: file']
        with mock.patch.object(self.tg, 'api', return_value={'ok': False, 'error_code': 502}):
            work.settle('Finished')
        self.assertEqual(len(self.rows()), 1)
        self.assertIn('done', json.loads(self.rows()[0]['params'])['text'])
        with mock.patch.object(self.tg, 'api', self.accept):
            work._publish()  # A late background publish after settlement.
        self.assertIn('done', self.accept.call_args.kwargs['text'])
        self.assertNotIn('working', self.accept.call_args.kwargs['text'])
        tgoutbox.retry(self.accept)
        self.assertEqual(self.rows(), [])

    def test_private_failed_status_and_reply_never_persist(self):
        self.tg._private['1:main'] = {'scratch': 'private'}
        work = self.tg.Working(1)
        work._msg_id = 20
        work._lines = ['private tool']
        with mock.patch.object(self.tg, 'api', return_value={'ok': False, 'error_code': 502}):
            work.settle('Private reply')
            self.tg.send(1, 'Private reply', durable=True)
        self.assertEqual(self.rows(), [])

    def test_duplicate_terminal_edit_is_one_pending_job(self):
        params = dict(chat_id=1, message_id=2, text='done')
        first = tgoutbox.enqueue('editMessageText', params, key='progress:1:2')
        second = tgoutbox.enqueue('editMessageText', params, key='progress:1:2')
        self.assertEqual(first, second)
        self.assertEqual(len(self.rows()), 1)

    def test_delivery_exception_keeps_reply_without_failing_completed_turn(self):
        with mock.patch.object(self.tg, 'api', side_effect=OSError), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.tg.send(1, 'Finished', durable=True), [])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['state'], 'pending')

    def test_delivery_lease_recovers_after_worker_crash(self):
        identifier = tgoutbox.enqueue('sendMessage', {'chat_id': 1, 'text': 'Ready'})
        with self.assertRaises(OSError):
            tgoutbox.deliver(identifier, mock.Mock(side_effect=OSError))
        tgoutbox.retry(self.accept)
        self.accept.assert_not_called()  # The active lease prevents duplicates.
        self.ready()  # Simulate the lease expiring after restart.
        tgoutbox.retry(self.accept)
        self.assertEqual(self.rows(), [])

    def test_flood_control_keeps_job_pending_until_retry_after(self):
        identifier = tgoutbox.enqueue('sendMessage', {'chat_id': 1, 'text': 'Ready'})
        with mock.patch.object(tgoutbox.time, 'time', return_value=100):
            tgoutbox.deliver(identifier, mock.Mock(return_value={
                'ok': False, 'error_code': 429, 'parameters': {'retry_after': 60}}))
            self.assertEqual(self.rows()[0]['next_attempt'], 160)
            tgoutbox.retry(self.accept)
        self.accept.assert_not_called()

    def test_additive_migration_preserves_existing_topic_state_and_pending_replies(self):
        path = ROOT / 'migrations/0011_telegram_outbox.py'
        spec = importlib.util.spec_from_file_location('telegram_outbox_migration_test', path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        self.con.execute("INSERT INTO collector_state (collector, cursor) VALUES ('telegram', ?)",
                         ('{"offset":42,"threads":{"old":"kept"}}',))
        tgoutbox.enqueue('sendMessage', {'chat_id': 1, 'text': 'Pending reply'})
        migration.apply(self.con)
        migration.apply(self.con)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.con.execute("SELECT cursor FROM collector_state WHERE collector='telegram'").fetchone()[0],
                         '{"offset":42,"threads":{"old":"kept"}}')

    def test_typing_failure_does_not_disable_general_or_double_send(self):
        work = self.tg.Working(-100)
        with mock.patch.object(self.tg, 'api', return_value={'ok': False, 'error_code': 429}) as api:
            work._send_typing()
        self.assertEqual(api.call_count, 1)
        self.assertTrue(work._typing_in_general)


if __name__ == '__main__':
    unittest.main()
