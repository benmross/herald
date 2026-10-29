"""Frozen regressions from memory failures: updates, provenance, quiet rebuilds."""

from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'lib'), str(ROOT)]
from cycles import _snapshot
from herald import changes, config, db


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
        self.assertIn('1 other fact record(s)', text)
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
        for i in range(12):
            self.fact(external_id=str(i), title=f'Meeting {i}')
        text, next_fp = _snapshot.delta(self.con, fp)
        self.assertIn('6 more named record changes', text)
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
        self.assertIn('1 other fact', text)

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
            self.assertIn('Memory document updated: state/project.md', text)
            changes.scan_documents(self.con)
            self.assertIsNone(_snapshot.delta(self.con, fp)[0])
            p.unlink()
            changes.scan_documents(self.con)
            self.assertIn('Memory document removed', _snapshot.delta(self.con, fp)[0])

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
