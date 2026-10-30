"""Search and session handoff, against a temporary ledger and fake transcripts."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))

from herald import changes, config, db, handoff, search, transcripts  # noqa: E402

BRIDGE = ('This message arrived from the user over Telegram.\n\n## Since your last message\n- noise\n'
          '---\n\nSent Friday.\n\nWhat did we decide about the boiler?')


class Search(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        for attr, value in [('LEDGER', self.root), ('STATE', self.root / 'state')]:
            p = mock.patch.object(config, attr, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(config, 'secrets', return_value={'x': {'token': 'hunter2-long-value'}})
        p.start()
        self.addCleanup(p.stop)
        (self.root / 'state/areas').mkdir(parents=True)
        (self.root / 'journal').mkdir()
        (self.root / 'state/areas/HOUSE.md').write_text(
            '# House\nIntro.\n\n## Heating\nThe boiler was serviced. key hunter2-long-value\n\n## Roof\nTiles.\n')
        (self.root / 'journal/2026-10-01.md').write_text('# 2026-10-01\n\n## Work\nReplaced the boiler valve.\n')
        self.con = sqlite3.connect(':memory:')
        self.con.row_factory = sqlite3.Row
        self.con.executescript(db.SCHEMA)
        db._migrate(self.con)
        self.addCleanup(self.con.close)
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        self.transcript = self.sessions / 'abc.jsonl'
        self.transcript.write_text('\n'.join(json.dumps(x) for x in [
            {'type': 'user', 'timestamp': '2026-10-02T10:00:00Z',
             'message': {'content': [{'type': 'text', 'text': BRIDGE}]}},
            {'type': 'assistant', 'timestamp': '2026-10-02T10:00:05Z',
             'message': {'content': [{'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'SECRETCOMMAND'}}]}},
            {'type': 'user', 'timestamp': '2026-10-02T10:00:06Z',
             'message': {'content': [{'type': 'tool_result', 'content': 'TOOLOUTPUT'}]}},
            {'type': 'assistant', 'timestamp': '2026-10-02T10:00:09Z',
             'message': {'content': [{'type': 'text', 'text': 'We kept the old boiler.'}]}},
        ]) + '\n')
        p = mock.patch.object(transcripts, 'locate',
                              side_effect=lambda ids=None: {'abc': self.transcript} if ids is None or 'abc' in ids else {})
        p.start()
        self.addCleanup(p.stop)
        self.con.execute("INSERT INTO runs(ts,label,engine,session_id) VALUES('t','telegram','claude','abc')")

    def index(self):
        return search.refresh(self.con)

    def test_notes_journals_and_conversations_are_found_with_a_way_back(self):
        self.index()
        found = search.find('boiler')
        kinds = {r['kind'] for r in found['results']}
        self.assertEqual(kinds, {'note', 'journal', 'conversation'})
        note = next(r for r in found['results'] if r['kind'] == 'note')
        self.assertIn('--section', note['ref'])

    def test_only_conversation_words_are_indexed_never_tool_traffic(self):
        self.index()
        self.assertEqual(search.find('SECRETCOMMAND')['results'], [])
        self.assertEqual(search.find('TOOLOUTPUT')['results'], [])
        self.assertEqual(search.find('noise')['results'], [])
        turn = next(r for r in search.find('boiler')['results'] if r['kind'] == 'conversation')
        self.assertIn('We kept the old boiler', search.show(turn['key']))

    def test_secrets_are_redacted_before_indexing(self):
        self.index()
        self.assertEqual(search.find('hunter2')['results'], [])
        clean = search.redactor()
        self.assertNotIn('ghp_', clean('token ghp_' + 'a' * 30))
        self.assertIn('[redacted]', clean('password: correcthorse'))

    def test_feed_events_are_not_indexed_and_quiet_runs_write_nothing(self):
        db.put_fact(self.con, 'gcal', 'event', external_id='a', title='Boiler fair', data={'role': 'feed'})
        db.put_fact(self.con, 'gcal', 'event', external_id='b', title='Boiler visit', data={'role': 'mine'})
        self.assertEqual(self.index()['facts'], 1)
        self.assertEqual(self.index(), {'notes': 0, 'facts': 0, 'conversation turns': 0})

    def test_a_removed_source_and_edited_note_leave_the_index(self):
        db.put_fact(self.con, 'gmail', 'message', external_id='m', title='Plumber quote', body='Boiler quote')
        self.index()
        self.con.execute("DELETE FROM facts WHERE external_id='m'")
        (self.root / 'state/areas/HOUSE.md').write_text('# House\nNo heating here.\n')
        (self.root / 'journal/2026-10-01.md').unlink()
        self.index()
        self.assertEqual({r['kind'] for r in search.find('boiler')['results']}, {'conversation'})

    def test_filters_and_any_word_fallback(self):
        self.index()
        self.assertEqual({r['kind'] for r in search.find('boiler', kinds=['journal'])['results']}, {'journal'})
        self.assertEqual(search.find('boiler', since='2027-01-01')['results'], [])
        self.assertIn('any word', search.find('boiler zebra')['matched'])

    def test_user_input_cannot_break_the_query(self):
        self.index()
        for query in ('boiler AND', 'NEAR(', '"unclosed boiler', 'title:boiler'):
            search.find(query)
        with self.assertRaises(ValueError):
            search.find('(*)')

    def test_missing_index_is_a_clear_error(self):
        with self.assertRaises(ValueError):
            search.find('anything')

    def test_handoff_quotes_where_the_session_stopped(self):
        text = handoff.brief(self.con, 'abc')
        self.assertIn('What did we decide about the boiler?', text)
        self.assertIn('We kept the old boiler.', text)
        self.assertNotIn('error', text)
        self.con.execute("UPDATE runs SET exit_code=1")
        self.assertIn('ended in an error', handoff.brief(self.con, 'abc'))

    def test_handoff_of_a_long_session_keeps_its_start_and_recent_turns(self):
        turns = [{'ts': 't', 'user': f'request {n}', 'reply': f'reply {n}'} for n in range(20)]
        with mock.patch.object(transcripts, 'turns', return_value=turns):
            text = handoff.brief(self.con, 'abc')
        self.assertIn('request 0', text)
        self.assertIn('request 19', text)
        self.assertIn('reply 15', text)
        self.assertNotIn('request 10', text)
        self.assertLess(len(text), 4000)

    def test_handoff_is_empty_without_a_session_or_after_three_days(self):
        self.assertEqual(handoff.brief(self.con, None), '')
        self.assertEqual(handoff.brief(self.con, 'abc', last_ts=1.0), '')


if __name__ == '__main__':
    unittest.main()
