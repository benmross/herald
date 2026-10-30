"""Compact retrieval must preserve status, provenance, privacy and expansion."""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'lib'), str(ROOT)]
from herald import config, context, db, private


class ContextReads(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        for attr, value in [('LEDGER', self.root), ('STATE', self.root/'state')]:
            p = mock.patch.object(config, attr, value)
            p.start()
            self.addCleanup(p.stop)
        (self.root/'state/areas').mkdir(parents=True)
        self.note = self.root/'state/areas/COURSE101.md'
        self.note.write_text('# Course 101\nCurrent course notes.\n\n## Contact\nInstructor: A. Teacher\n\n## Assessment policy\nRead the original policy.\n### Absences\nDocumentation required.\n```python\n# Not a heading\n```\n\n## Open questions\nExam location unconfirmed.\n')
        self.con = sqlite3.connect(':memory:')
        self.con.row_factory = sqlite3.Row
        self.con.executescript(db.SCHEMA)
        db._migrate(self.con)
        self.addCleanup(self.con.close)
        self.today = dt.datetime.now(config.tz()).date()
        self.tomorrow = (self.today + dt.timedelta(days=1)).isoformat()

    def deadline(self, title='COURSE101 Exam', status='live', due=None):
        self.con.execute("INSERT INTO deadlines(source,kind,title,due,status,created_at,updated_at) VALUES('manual','test',?,?,?,'now','now')",
                         (title, due or self.tomorrow, status))

    def output(self, **kwargs):
        return context.render(context.packet(self.con, **kwargs))[0]

    def test_topic_alias_spacing_and_current_rows_in_one_read(self):
        self.deadline()
        self.assertEqual(context.resolve('Course 101')[0]['topic'], 'COURSE101')
        text = self.output(topic='COURSE101')
        self.assertIn('Current course notes', text)
        self.assertIn('Instructor: A. Teacher', text)
        self.assertIn('COURSE101 Exam', text)
        self.assertIn('Read the original policy', text)
        self.assertNotIn('Status counts', text)

    def test_section_parent_includes_nested_policy_but_not_next_section(self):
        text = self.output(topic='COURSE101', section='Assessment policy')
        self.assertIn('Documentation required', text)
        self.assertNotIn('Exam location unconfirmed', text)
        self.assertNotIn('Instructor:', text)
        headings = context.sections(self.note)
        self.assertNotIn('Not a heading', [s['heading'] for s in headings])

    def test_ambiguous_heading_requires_explicit_key(self):
        self.note.write_text('# Course\n## Policy\nOne\n## Policy\nTwo\n')
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.output(topic='COURSE101', section='Policy')
        self.assertIn('Two', self.output(topic='COURSE101', section='s4'))

    def test_section_offset_is_relative_and_carries_actual_source_line(self):
        text = self.output(topic='COURSE101', section='Assessment policy', offset=1)
        self.assertIn('COURSE101.md:8', text)
        self.assertNotIn('\n## Assessment policy\n', text)

    def test_live_deadlines_exclude_cancelled_and_past_without_mutating(self):
        self.deadline()
        self.deadline('Cancelled exam', 'cancelled')
        self.deadline('Earlier exam', due='2000-01-01')
        text = self.output(view='deadlines')
        self.assertIn('COURSE101 Exam', text)
        self.assertNotIn('Cancelled exam', text)
        self.assertNotIn('Earlier exam', text)
        all_rows = context.operational(self.con, 'deadlines', all_statuses=True)
        self.assertEqual(all_rows['total'], 3)
        self.assertEqual(self.con.execute("SELECT count(*) FROM deadlines WHERE status='live'").fetchone()[0], 2)

    def test_open_commitments_include_overdue_and_status_counts_are_historical(self):
        self.con.execute("INSERT INTO commitments(created_at,text,due) VALUES('now','Send the draft','2000-01-01')")
        self.con.execute("INSERT INTO commitments(created_at,text,status) VALUES('now','Completed task','done')")
        self.assertIn('Send the draft', self.output(view='commitments'))
        self.assertNotIn('Completed task', self.output(view='commitments'))
        self.assertIn('"done": 1', self.output(view='counts'))

    def test_saved_application_is_never_relabelled_submitted(self):
        self.con.execute("INSERT INTO applications(url,company,title,status,created_at) VALUES('https://example.org/role','Example','Intern','filled','now')")
        text = self.output(view='applications')
        self.assertIn('"status": "filled"', text)
        self.assertIn('"submitted_at": null', text)

    def test_personal_schedule_respects_local_dates_and_rejects_feeds(self):
        zone = ZoneInfo('America/Los_Angeles')
        with mock.patch.object(config, 'tz', return_value=zone):
            today = dt.datetime.now(zone).date()
            early = dt.datetime.combine(today, dt.time(0, 30), zone).astimezone(dt.timezone.utc)
            previous = early - dt.timedelta(hours=2)
            for key, role, timestamp in [('ours', 'mine', early.isoformat()),
                                          ('public', 'feed', early.isoformat()),
                                          ('yesterday', 'mine', previous.isoformat()),
                                          ('all-day', 'mine', today.isoformat())]:
                db.put_fact(self.con, 'calendar', 'event', external_id=key, title=key,
                            ts=timestamp, data={'role': role})
            result = context.operational(self.con, 'schedule', days=1)
            titles = [json.loads(r['text'])['title'] for r in result['items']]
            self.assertCountEqual(titles, ['ours', 'all-day'])

    def test_record_paging_and_whole_record_expansion(self):
        for title in ['Exam A', 'Exam B', 'Exam C']:
            self.deadline(title)
        result = context.operational(self.con, 'deadlines', limit=1, offset=1)
        self.assertTrue(result['has_more'])
        self.assertEqual(len(result['items']), 1)
        self.assertIn('--id', result['items'][0]['expand'])
        self.assertIn('"notes"', result['items'][0]['text'])

    def test_topic_filter_is_literal_and_unknown_topic_does_not_invent_notes(self):
        self.deadline()
        result = context.operational(self.con, 'deadlines', topic="' OR 1=1 --")
        self.assertEqual(result['total'], 0)
        text = self.output(topic='UnrecordedProject')
        self.assertIn('No maintained note has this name', text)
        self.assertNotIn('Current course notes', text)

    def test_paths_cannot_escape_ledger_even_through_symlinks(self):
        with self.assertRaises(ValueError):
            context.ledger_path('../../elsewhere.md')
        outside = self.root.parent/'outside-context-test.md'
        target = self.root/'state/areas/escape.md'
        target.symlink_to(outside)
        with self.assertRaises(ValueError):
            context.ledger_path('state/areas/escape.md')

    def test_hard_byte_budget_and_explicit_source_for_large_unicode_excerpt(self):
        self.note.write_text('# Course 101\n## Policy\n' + ('政策と例\n' * 3000))
        result = context.packet(self.con, topic='COURSE101', section='Policy')
        text, _, more = context.render(result, 256)
        self.assertLessEqual(len(text.encode()), 1024)
        self.assertTrue(more)
        self.assertIn('excerpt', text)
        self.assertIn('--budget 0', text)

    def test_alias_routes_and_finds_rows_filed_under_the_code(self):
        self.note.write_text('# Course 101\n<!-- aliases: intro stuff, c101 -->\nNotes.\n')
        self.deadline()
        self.assertEqual(context.resolve('Intro Stuff')[0]['topic'], 'COURSE101')
        self.assertIn('COURSE101 Exam', self.output(topic='intro stuff'))

    def test_a_short_note_comes_back_whole_without_lexical_row_noise(self):
        (self.root/'state/now.md').write_text('# Now\n\n## Today: Friday\nExam at noon.\n')
        self.con.execute("INSERT INTO commitments(created_at,text) VALUES('now','Know the snow rules')")
        text = self.output(topic='now')
        self.assertIn('Exam at noon', text)
        self.assertNotIn('snow rules', text)

    def test_long_note_previews_and_lists_its_sections(self):
        self.note.write_text('# Course 101\nIntro.\n\n## Assessment policy\n' + 'Policy line.\n' * 500)
        text = self.output(topic='COURSE101')
        self.assertIn('s4 Assessment policy', text)
        self.assertNotIn('Policy line.', text)

    def test_topic_index_is_complete_in_one_call(self):
        for i in range(30):
            (self.root/f'state/areas/T{i}.md').write_text('# T\n')
        self.assertIn('T29', self.output(topic='topics'))

    def test_lint_finds_a_dangling_reference_and_a_stale_note(self):
        self.note.write_text('# Course 101\nSee `documents/gone.md`.\n')
        os.utime(self.note, (0, 0))
        found = '\n'.join(context.lint())
        self.assertIn('documents/gone.md', found)
        self.assertIn('no Verified line', found)
        self.note.write_text('# Course 101\nVerified: 2026-10-30, syllabus.\n')
        os.utime(self.note, (0, 0))
        self.assertEqual(context.lint(), [])

    def test_partial_source_warning_survives_context(self):
        db.mark_collector(self.con, 'calendar', ok=True, partial=True, pending=2)
        text = self.output(topic='COURSE101')
        self.assertIn('calendar: partial source coverage', text)
        self.assertIn('pending retries', text)

    def test_relevant_schema_does_not_dump_other_tables(self):
        text = self.output(view='applications', schema=True)
        self.assertIn('submitted_at', text)
        self.assertNotIn('[commitments]', text)

    def test_metrics_store_metadata_only_and_correlate_additional_calls(self):
        with mock.patch.dict(os.environ, {'HERALD_RETRIEVAL_TURN': 'opaque-test-invocation'}):
            first = context.record(self.con, 'section', time.perf_counter(), 'private source text', 1, False)
            second = context.record(self.con, 'section', time.perf_counter(), 'other private source text', 1, False)
        row = self.con.execute('SELECT * FROM retrievals WHERE id=?', (second,)).fetchone()
        self.assertEqual(row['parent_id'], first)
        self.assertNotIn('private source text', str(dict(row)))
        self.assertNotIn('opaque-test-invocation', str(dict(row)))
        self.assertIn('followup_calls', context.stats(self.con))

    def test_private_reads_never_record_or_refresh(self):
        before = self.con.total_changes
        with mock.patch.dict(os.environ, {private.ENV: '1'}):
            self.assertIn('Current course notes', self.output(topic='COURSE101'))
            self.assertIsNone(context.record(self.con, 'topic', time.perf_counter(), 'text', 1, False))
            with self.assertRaises(private.Refused):
                context.refresh_opportunities(self.con)
        self.assertEqual(self.con.total_changes, before)

    def test_opportunity_narrative_is_archived_byte_for_byte_before_compaction(self):
        previous = b'# Opportunities\nHistorical reasoning that has not been discarded.\n' * 3000
        path = self.root/'state/opportunities.md'
        path.write_bytes(previous)
        archive = context.refresh_opportunities(self.con)
        self.assertEqual(archive.read_bytes(), previous)
        self.assertLess(path.stat().st_size, 4000)
        self.assertIsNone(context.refresh_opportunities(self.con))
        self.assertEqual(len(list((self.root/'documents/archive').glob('*.md'))), 1)

    def test_unchanged_overview_does_not_change_when_only_time_passes(self):
        context.refresh_opportunities(self.con)
        path = self.root/'state/opportunities.md'
        before, mtime = path.read_bytes(), path.stat().st_mtime_ns
        original = dt.datetime
        class Later(original):
            @classmethod
            def now(cls, tz=None):
                return original.now(tz) + dt.timedelta(days=1)
        with mock.patch.object(context.dt, 'datetime', Later):
            context.refresh_opportunities(self.con)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_small_budget_still_names_incomplete_sources(self):
        for i in range(12):
            db.mark_collector(self.con, f'source{i}', ok=True, partial=True, pending=1)
        text, _, _ = context.render(context.packet(self.con, topic='COURSE101'), 256)
        self.assertIn('Incomplete sources: source0', text)
        self.assertLessEqual(len(text.encode()), 1024)


if __name__ == '__main__':
    unittest.main()
