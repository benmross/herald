"""Frozen regressions from memory failures: updates, provenance, quiet rebuilds."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'lib'), str(ROOT)]
from cycles import _snapshot
from herald import changes, mailreadings, config, db, migrations


class MemoryRevisions(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(':memory:')
        self.con.row_factory = sqlite3.Row
        self.con.executescript(db.SCHEMA)
        db._migrate(self.con)
        self.addCleanup(self.con.close)

    def fact(self, **kwargs):
        values = dict(external_id='meeting', ts='2026-10-30T10:00:00+00:00',
                      title='Project meeting', data={'role': 'mine'})
        values.update(kwargs)
        return db.put_fact(self.con, 'calendar', 'event', **values)

    def baseline(self):
        return _snapshot.delta(self.con, None)[1]

    def test_meeting_moved_in_place_is_reported(self):
        original = self.fact()
        fp = self.baseline()
        self.assertEqual(original, self.fact(ts='2026-10-30T11:00:00+00:00'))
        text, _ = _snapshot.delta(self.con, fp)
        self.assertIn('11:00', text)
        self.assertIn('updated', text)

    def test_identical_fetch_and_key_order_changes_are_quiet(self):
        self.fact(data={'role': 'mine', 'location': 'Building A'})
        fp = self.baseline()
        self.fact(data={'location': 'Building A', 'role': 'mine'})
        self.assertIsNone(_snapshot.delta(self.con, fp)[0])

    def test_direct_sql_edit_and_deletion_preserve_evidence(self):
        fid = self.fact()
        fp = self.baseline()
        self.con.execute("UPDATE facts SET title='Renamed meeting' WHERE id=?", (fid,))
        text, fp = _snapshot.delta(self.con, fp)
        self.assertIn('Renamed meeting', text)
        self.con.execute('DELETE FROM facts WHERE id=?', (fid,))
        text, _ = _snapshot.delta(self.con, fp)
        self.assertIn('removed: Renamed meeting', text)

    def test_sparse_ids_count_records(self):
        fp = self.baseline()
        self.con.execute("INSERT INTO facts(id,source,kind,title,ingested_at) "
                         "VALUES(10001,'example','reading','A reading','now')")
        text, _ = _snapshot.delta(self.con, fp)
        self.assertIn('1 other record(s)', text)
        self.assertNotIn('10001 other', text)

    def test_rebuilt_identical_derived_facts_are_quiet(self):
        self.fact()
        fp = self.baseline()
        db.clear(self.con, 'calendar', 'event')
        self.fact()
        self.assertIsNone(_snapshot.delta(self.con, fp)[0])

    def test_reverted_edit_is_quiet(self):
        self.fact()
        fp = self.baseline()
        self.fact(title='Temporary correction')
        self.fact()
        self.assertIsNone(_snapshot.delta(self.con, fp)[0])

    def test_body_change_counts_even_with_same_title(self):
        self.fact(body='Old instructions')
        fp = self.baseline()
        self.fact(body='Corrected instructions')
        self.assertIn('updated', _snapshot.delta(self.con, fp)[0])

    def test_commitment_due_date_and_deadline_cancellation(self):
        self.con.execute("INSERT INTO commitments(created_at,text,due) VALUES "
                         "('now','Send the draft','2026-10-30')")
        self.con.execute("INSERT INTO deadlines(source,kind,title,due,created_at,updated_at) "
                         "VALUES('manual','test','Course exam','2026-11-04','now','now')")
        fp = self.baseline()
        self.con.execute("UPDATE commitments SET due='2026-10-31'")
        self.con.execute("UPDATE deadlines SET status='cancelled'")
        text, _ = _snapshot.delta(self.con, fp)
        self.assertIn('2026-10-31', text)
        self.assertIn('Course exam', text)
        self.assertIn('cancelled', text)

    def test_large_batch_has_explicit_overflow_and_full_cursor(self):
        fp = self.baseline()
        for i in range(18):
            self.fact(external_id=str(i), title=f'Meeting {i}')
        text, next_fp = _snapshot.delta(self.con, fp)
        self.assertIn('6 more record changes', text)
        self.assertEqual(next_fp['change_seq'], changes.sequence(self.con))
        self.assertIsNone(_snapshot.delta(self.con, next_fp)[0])

    def test_old_topic_upgrades_without_fabricating_counts(self):
        text, fp = _snapshot.delta(self.con, {'fact_id': 9999, 'open_n': 20})
        self.assertIn('Revision tracking is now active', text)
        self.assertNotIn('closed or dropped', text)
        self.assertIn('change_seq', fp)

    def test_transaction_rollback_removes_revisions_too(self):
        fp = self.baseline()
        self.con.commit()
        self.fact()
        self.con.rollback()
        self.assertIsNone(_snapshot.delta(self.con, fp)[0])

    def test_feed_listing_never_becomes_your_event(self):
        fp = self.baseline()
        self.fact(data={'role': 'feed'})
        text, _ = _snapshot.delta(self.con, fp)
        self.assertNotIn('Your event', text)
        self.assertIn('1 other record(s)', text)

    def test_rebuilds_are_coalesced_per_reader_without_deleting_history(self):
        self.fact()
        self.fact(external_id='other', title='Other')
        start = changes.sequence(self.con)
        db.clear(self.con, 'calendar', 'event')
        self.fact()
        self.fact(external_id='other', title='Renamed')
        self.assertEqual(changes.compact(self.con, start), 0)
        left = self.con.execute('SELECT count(*) FROM ledger_changes WHERE seq>?', (start,)).fetchone()[0]
        self.assertEqual(left, 4)
        self.assertEqual(len(changes.net(self.con, start)), 1)

    def test_compaction_preserves_correction_for_reader_that_saw_intermediate_state(self):
        self.fact(title='A')
        self.con.execute('DELETE FROM ledger_changes')
        self.fact(title='B')
        reader = changes.sequence(self.con)
        self.fact(title='A')
        changes.compact(self.con)
        pending = changes.net(self.con, reader)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['before']['title'], 'B')
        self.assertEqual(pending[0]['after']['title'], 'A')

    def test_compaction_coverage_migration_warns_old_readers_once(self):
        self.fact(title='Before migration')
        fingerprint = self.baseline()
        self.fact(title='After old cursor')
        migration = migrations._load(ROOT/'migrations/0009_compaction_coverage.py')
        migration.apply(self.con)
        through = changes.pruned_through(self.con)
        self.fact(title='Later change')
        migration.apply(self.con)
        self.assertEqual(changes.pruned_through(self.con), through)
        self.assertIn('history for this conversation is incomplete', _snapshot.delta(self.con, fingerprint)[0])

    def test_prune_forgets_old_revisions_and_warns_an_idle_conversation(self):
        self.fact()
        fingerprint = self.baseline()
        self.con.execute("UPDATE ledger_changes SET changed_at='2020-01-01T00:00:00.000Z'")
        self.assertEqual(changes.prune(self.con, 30), 1)
        fingerprint['change_seq'] = 0
        self.con.execute("INSERT INTO memory_metadata VALUES ('pruned_through','1') "
                         "ON CONFLICT(key) DO UPDATE SET value='1'")
        self.fact(title='Moved')
        text, _ = _snapshot.delta(self.con, fingerprint)
        self.assertIn('Change history for this conversation is incomplete', text)

    def test_a_due_date_change_is_not_cut_by_newsletters(self):
        fingerprint = self.baseline()
        for n in range(10):
            db.put_fact(self.con, 'gmail', 'message', external_id=f'news{n}', title=f'Weekly digest {n}',
                        data={'from': 'News <n@example.org>', 'bulk': True})
        db.put_fact(self.con, 'gmail', 'message', external_id='due', title='Assignment Due Date Changed: Essay',
                    data={'from': 'Course <c@example.org>', 'bulk': True})
        text, _ = _snapshot.delta(self.con, fingerprint)
        self.assertIn('Due Date Changed', text)
        self.assertNotIn('Weekly digest', text)
        self.assertIn('10 bulk email', text)

    def test_records_outrank_mail_however_many_arrived(self):
        fingerprint = self.baseline()
        for n in range(20):
            db.put_fact(self.con, 'gmail', 'message', external_id=f'm{n}', title=f'Hello {n}',
                        data={'from': 'A Person <a@example.org>'})
        self.fact(title='Moved meeting')
        text, _ = _snapshot.delta(self.con, fingerprint)
        self.assertLess(text.index('Moved meeting'), text.index('Hello'))

    def test_application_status_change_reaches_a_conversation(self):
        self.con.execute("INSERT INTO applications(url,created_at,company,title,status) VALUES ('u','t','Example','Intern','submitted')")
        fingerprint = self.baseline()
        self.con.execute("UPDATE applications SET status='rejected'")
        text, _ = _snapshot.delta(self.con, fingerprint)
        self.assertIn('Application updated: Intern at Example; status rejected', text)

    def test_routine_writes_collapse_to_one_line(self):
        fingerprint = self.baseline()
        for n in range(30):
            self.con.execute("INSERT INTO actions(ts,actor,tier,kind,target,summary) VALUES ('t','test','amber','calendar.create','Campus',?)", (str(n),))
        text, _ = _snapshot.delta(self.con, fingerprint)
        self.assertIn('Herald wrote 30 times', text)

    def test_cancelled_meeting_reaches_the_brief_as_cancelled(self):
        today = dt.date.today()
        ts = f'{today + dt.timedelta(days=1)}T17:30:00-04:00'
        self.fact(external_id='weekly', ts=ts, title='Team meeting')
        self.fact(external_id='past', ts='2020-01-01T10:00:00-04:00', title='Old')
        since = '2000-01-01T00:00:00+00:00'
        self.con.execute('DELETE FROM ledger_changes')
        self.con.execute("DELETE FROM facts WHERE external_id IN ('weekly','past')")
        lines = _snapshot._calendar_changes(self.con, since, today)
        self.assertEqual(len(lines), 1)
        self.assertIn('CANCELLED or removed: Team meeting', lines[0])

    def test_moved_event_is_reported_with_both_times(self):
        today = dt.date.today()
        first = f'{today + dt.timedelta(days=1)}T10:00:00-04:00'
        self.fact(ts=first)
        seq = changes.sequence(self.con)
        self.fact(ts=f'{today + dt.timedelta(days=2)}T10:00:00-04:00')
        self.con.execute('DELETE FROM ledger_changes WHERE seq<=?', (seq,))
        lines = _snapshot._calendar_changes(self.con, '2000-01-01T00:00:00+00:00', today)
        self.assertIn('MOVED: Project meeting', lines[0])

    def test_a_reading_replaces_the_message_text_and_outlives_it(self):
        db.put_fact(self.con, 'gmail', 'message', external_id='m1', ts='2026-10-29T12:00:00-04:00',
                    title='Thank you', body='Long text', data={'from': 'x', 'body_format': 'text'})
        fact = self.con.execute("SELECT * FROM facts WHERE external_id='m1'").fetchone()
        kept = mailreadings.save(self.con, [
            {'message_id': mailreadings.reading_id(fact), 'says': 'They declined the application.', 'matters': 'fyi'},
            {'message_id': 'gmail:invented', 'says': 'Nothing', 'matters': 'no'}], 'cycle:scout')
        self.assertEqual(kept, 1)
        fact = self.con.execute("SELECT * FROM facts WHERE external_id='m1'").fetchone()
        found = mailreadings.lookup(self.con, [fact])
        self.assertEqual(found['gmail:m1']['says'], 'They declined the application.')
        self.assertEqual(found['gmail:m1']['read_by'], 'cycle:scout')

    def test_reading_invalidates_after_body_correction_and_rejects_inflight_old_version(self):
        db.put_fact(self.con, 'gmail', 'message', external_id='m', body='Old', data={'body_format':'text'})
        fact = self.con.execute("SELECT * FROM facts WHERE source='gmail'").fetchone()
        entry = {'message_id': mailreadings.reading_id(fact), 'says': 'Old decision', 'matters':'fyi'}
        self.assertEqual(mailreadings.save(self.con, [entry], 'test'), 1)
        self.con.execute("UPDATE facts SET body='Correction' WHERE source='gmail'")
        fact = self.con.execute("SELECT * FROM facts WHERE source='gmail'").fetchone()
        self.assertEqual(mailreadings.lookup(self.con, [fact]), {})
        self.assertEqual(mailreadings.save(self.con, [entry], 'test'), 0)

    def test_legacy_and_incomplete_readings_are_not_reused(self):
        db.put_fact(self.con, 'gmail', 'message', external_id='m', body='Text', data={'body_format':'text'})
        fact = self.con.execute("SELECT * FROM facts WHERE source='gmail'").fetchone()
        self.assertEqual(mailreadings.save(self.con, [{'message_id':'gmail:m','says':'Old'}], 'test'), 0)
        db.put_fact(self.con, 'mail_reading', 'message', external_id='gmail:m', body='Legacy summary')
        self.assertEqual(mailreadings.lookup(self.con, [fact]), {})
        self.assertIsNone(mailreadings.reading_id(fact, complete=False))
        for data in ({}, {'body_format':'text','body_truncated':True}, {'body_format':'none'}):
            value = dict(fact, data=json.dumps(data))
            self.assertIsNone(mailreadings.reading_id(value))

    def test_budget_held_back_mail_does_not_get_a_reading_token(self):
        db.put_fact(self.con, 'gmail', 'message', external_id='m', ts=db.now(), body='Full text',
                    data={'body_format':'text','from_me':False})
        with mock.patch.object(_snapshot, 'UNREAD_TOTAL_CHARS', 0):
            text, _ = _snapshot.build(self.con, 'dawn')
        self.assertIn('text held back', text)
        self.assertNotIn('reading id: gmail:m#', text)

    def test_clipped_mail_is_ineligible_but_complete_text_gets_a_version_token(self):
        db.put_fact(self.con, 'gmail', 'message', external_id='m', ts=db.now(), body='Full text',
                    data={'body_format':'text','from_me':False})
        with mock.patch.object(_snapshot, 'UNREAD_DIRECT_CHARS', 4):
            clipped, _ = _snapshot.build(self.con, 'dawn')
        self.assertIn('4 of 9 characters', clipped)
        self.assertNotIn('reading id: gmail:m#', clipped)
        full, _ = _snapshot.build(self.con, 'dawn')
        self.assertIn('reading id: gmail:m#', full)

    def test_documents_baseline_edit_delete_and_unchanged_scan(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(config, 'LEDGER', pathlib.Path(directory)):
            root = pathlib.Path(directory) / 'state'
            root.mkdir()
            p = root / 'project.md'
            p.write_text('Original conclusion')
            changes.scan_documents(self.con)
            fp = self.baseline()
            p.write_text('Corrected conclusion')
            changes.scan_documents(self.con)
            text, fp = _snapshot.delta(self.con, fp)
            self.assertIn('Memory documents changed: state/project.md', text)
            changes.scan_documents(self.con)
            self.assertIsNone(_snapshot.delta(self.con, fp)[0])
            p.unlink()
            changes.scan_documents(self.con)
            self.assertIn('(removed)', _snapshot.delta(self.con, fp)[0])

    def test_private_mode_does_not_scan_documents(self):
        with mock.patch.object(changes.private, 'active', return_value=True):
            changes.scan_documents(self.con)
        self.assertEqual(self.con.execute("SELECT count(*) FROM memory_metadata "
                                          "WHERE key='documents_baselined'").fetchone()[0], 0)

    def test_health_preserves_last_complete_until_recovery(self):
        db.mark_collector(self.con, 'example', ok=True, cursor='old')
        last = self.con.execute("SELECT last_complete FROM collector_state").fetchone()[0]
        fp = self.baseline()
        db.mark_collector(self.con, 'example', ok=True, partial=True, pending=2,
                          error='Some pages unavailable', coverage={'start': '2026-10-01'})
        text, fp = _snapshot.delta(self.con, fp)
        self.assertIn('partial source coverage', text)
        self.assertEqual(self.con.execute('SELECT last_complete FROM collector_state').fetchone()[0], last)
        self.assertIsNone(_snapshot.delta(self.con, fp)[0])
        db.mark_collector(self.con, 'example', ok=True, cursor='new')
        self.assertIn('coverage recovered', _snapshot.delta(self.con, fp)[0])

    def test_install_does_not_claim_legacy_sync_was_complete(self):
        self.con.execute("INSERT INTO collector_state(collector,last_ok) VALUES('legacy','yesterday')")
        db._migrate(self.con)
        state = self.con.execute("SELECT * FROM collector_state WHERE collector='legacy'").fetchone()
        self.assertEqual(state['sync_status'], 'unknown')
        self.assertIsNone(state['last_complete'])

    def test_reconciled_snapshot_does_not_grow_history_on_quiet_run(self):
        rows = [dict(external_id='a', title='Thread A'),
                dict(external_id='b', title='Thread B')]
        db.replace_facts(self.con, 'example', 'thread', rows)
        seq = changes.sequence(self.con)
        db.replace_facts(self.con, 'example', 'thread', rows)
        self.assertEqual(changes.sequence(self.con), seq)
        db.replace_facts(self.con, 'example', 'thread', rows[:1])
        revisions = changes.net(self.con, seq)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]['before']['title'], 'Thread B')
        self.assertIsNone(revisions[0]['after'])


if __name__ == '__main__':
    unittest.main()
