"""Mailbox access must preserve read flags, checkpoints and draft-only writes."""
import contextlib
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'lib'))
from herald import purelymail

RAW = (b'From: Person <person@example.org>\r\nTo: me@example.org\r\n'
       b'Subject: Hello\r\nMessage-ID: <one@example.org>\r\n'
       b'Date: Wed, 28 Oct 2026 12:00:00 +0000\r\n\r\nHello there.\r\n')


class Client:
    def __init__(self, validity=b'7', uids=b'1 2', fail=False):
        self.validity, self.uids, self.fail = validity, uids, fail
        self.calls = []

    def list(self):
        return 'OK', [b'(\\HasNoChildren) "/" INBOX',
                      b'(\\Drafts) "/" "My Drafts"', b'(\\Noselect) "/" Parent']

    def select(self, folder, readonly=False):
        self.calls.append(('select', folder, readonly))
        return 'OK', [b'2']

    def response(self, key):
        return key, [self.validity]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == 'SEARCH':
            return 'OK', [self.uids]
        if self.fail:
            return 'NO', []
        return 'OK', [(b'data', RAW.replace(b'<one@', f'<{args[0]}@'.encode()))]

    def append(self, folder, flags, date, raw):
        self.calls.append(('append', folder, flags, raw))
        return 'OK', [b'APPENDUID 7 3']


class Connection:
    def commit(self):
        pass


class Purelymail(unittest.TestCase):
    def setting(self, key, default=None):
        return {'purelymail.address': 'me@example.org',
                'purelymail.batch_per_folder': 1}.get(key, default)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for p in [patch.object(purelymail.config, 'get', side_effect=self.setting),
                  patch.object(purelymail.config, 'LEDGER', pathlib.Path(self.temp.name))]:
            p.start()
            self.addCleanup(p.stop)

    def collect(self, client, cursor=None):
        with patch.object(purelymail, 'mailbox', return_value=contextlib.nullcontext(client)), \
             patch.object(purelymail.collector, 'cursor', return_value=cursor), \
             patch.object(purelymail.db, 'put_fact') as put:
            result = purelymail.collect(Connection())
            return result, put

    def test_first_sync_is_bounded_readonly_and_peek(self):
        client = Client()
        result, put = self.collect(client)
        self.assertEqual(result['messages'], 1)
        self.assertEqual(put.call_count, 1)
        self.assertIn(('select', 'INBOX', True), client.calls)
        self.assertIn(('FETCH', '1', '(BODY.PEEK[])'), client.calls)
        state = json.loads(result['_cursor'])
        self.assertEqual(state['folders']['INBOX'], {'validity': '7', 'uid': 1})
        self.assertNotIn('"My Drafts"', state['folders'])

    def test_incremental_filters_imap_star_returning_last_old_uid(self):
        cursor = json.dumps({'account': 'me@example.org', 'folders': {'INBOX': {'validity': '7', 'uid': 2}}})
        client = Client(uids=b'2')
        result, put = self.collect(client, cursor)
        self.assertEqual(result['messages'], 0)
        put.assert_not_called()
        self.assertIn(('SEARCH', None, 'UID', '3:*'), client.calls)

    def test_uidvalidity_change_resets_to_recent_scan(self):
        cursor = json.dumps({'account': 'me@example.org', 'folders': {'INBOX': {'validity': '6', 'uid': 200}}})
        client = Client()
        result, put = self.collect(client, cursor)
        self.assertEqual(result['messages'], 1)
        self.assertEqual(client.calls[1][2], 'SINCE')

    def test_failed_fetch_never_returns_a_new_checkpoint(self):
        with self.assertRaises(RuntimeError):
            self.collect(Client(fail=True))

    def test_message_identity_survives_folder_move(self):
        first = purelymail.parse_message(RAW, folder='INBOX', uid=1, validity='7')
        moved = purelymail.parse_message(RAW, folder='Archive', uid=99, validity='8')
        self.assertEqual(first[0], moved[0])
        self.assertEqual(first[3].strip(), 'Hello there.')
        self.assertFalse(first[4]['from_me'])

    def test_draft_uses_special_folder_logs_and_preserves_reply_headers(self):
        client = Client()
        with patch.object(purelymail, 'mailbox', return_value=contextlib.nullcontext(client)), \
             patch.object(purelymail.db, 'record_action') as log:
            result = purelymail.draft(Connection(), to='person@example.org', subject='Re: Hello',
                                      body='Reply', in_reply_to='<one@example.org>')
        self.assertEqual(result['folder'], '"My Drafts"')
        call = client.calls[-1]
        self.assertEqual(call[0:3], ('append', '"My Drafts"', r'(\Draft)'))
        self.assertIn(b'In-Reply-To: <one@example.org>', call[3])
        log.assert_called_once()

    def test_private_mode_refuses_before_network(self):
        with patch.dict('os.environ', HERALD_PRIVATE='1'), \
             patch.object(purelymail, 'mailbox') as network:
            with self.assertRaises(PermissionError):
                purelymail.draft(Connection(), to='person@example.org', subject='Hi', body='Hi')
            network.assert_not_called()


if __name__ == '__main__':
    unittest.main()
