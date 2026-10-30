"""An item that leaves a calendar feed must stop asserting its date."""

from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import sqlite3
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))

from herald import db, deadlines, gwrite  # noqa: E402

spec = importlib.util.spec_from_file_location('calendar_feeds', ROOT / 'collectors/calendar_feeds.py')
feeds = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feeds)


class FeedRemovals(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(':memory:')
        self.con.row_factory = sqlite3.Row
        self.con.executescript(db.SCHEMA)
        db._migrate(self.con)
        self.addCleanup(self.con.close)
        self.day = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        patcher = mock.patch.object(deadlines, '_cfg', return_value={'feeds': [{'source': 'lms'}]})
        patcher.start()
        self.addCleanup(patcher.stop)

    def item(self, uid, ts=None):
        db.put_fact(self.con, 'lms', 'assignment', external_id=uid, ts=ts or self.day, title=uid)

    def test_removed_item_goes_and_a_past_item_stays(self):
        for uid in ('a', 'b', 'c', 'd'):
            self.item(uid)
        self.item('old', ts='2020-01-01')
        self.assertEqual(feeds._forget_removed(self.con, 'lms', {'a', 'b', 'c'}), 1)
        left = {r[0] for r in self.con.execute('SELECT external_id FROM facts')}
        self.assertEqual(left, {'a', 'b', 'c', 'old'})

    def test_a_feed_that_lost_most_of_its_rows_removes_nothing(self):
        for n in range(20):
            self.item(str(n))
        self.assertEqual(feeds._forget_removed(self.con, 'lms', {'0'}), 0)
        self.assertEqual(feeds._forget_removed(self.con, 'lms', set()), 0)

    def test_deadline_is_cancelled_and_revives_if_the_item_returns(self):
        rid = deadlines.add(self.con, source='lms', ref='a', course='C', kind='due', title='Essay', due=self.day)
        self.assertEqual(deadlines.retire_removed(self.con), 1)
        row = self.con.execute('SELECT status,notes FROM deadlines WHERE id=?', (rid,)).fetchone()
        self.assertEqual(row['status'], 'cancelled')
        deadlines.add(self.con, source='lms', ref='a', course='C', kind='due', title='Essay', due=self.day)
        row = self.con.execute('SELECT status,notes FROM deadlines WHERE id=?', (rid,)).fetchone()
        self.assertEqual((row['status'], row['notes']), ('live', None))

    def test_mail_about_a_changed_date_flags_the_row(self):
        rid = deadlines.add(self.con, source='lms', ref='a', course='FIRE120', kind='due',
                            title='FIRE120 Stream Outcome', due=self.day)
        other = deadlines.add(self.con, source='lms', ref='b', course='FIRE120', kind='due',
                              title='FIRE120 Other Thing', due=self.day)
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
        db.put_fact(self.con, 'gmail', 'message', external_id='m', ts=now,
                    title='Assignment Due Date Changed: Stream Outcome, FIRE120')
        rows = self.con.execute('SELECT id,title,course FROM deadlines').fetchall()
        found = deadlines.disputes(self.con, rows)
        self.assertIn(rid, found)
        self.assertNotIn(other, found)

    def test_hash_survives_an_event_with_empty_fields(self):
        """A live event read back with no colour or location must not crash the cancel."""
        existing = {'summary': 'Essay', 'start': '2026-10-30', 'end': '2026-10-31',
                    'location': None, 'description': None, 'colorId': None}
        projected = gwrite._projected(existing, {'summary': 'CANCELLED: Essay'})
        self.assertEqual(gwrite.field_hash(projected), gwrite.field_hash(
            {'summary': 'CANCELLED: Essay', 'start': {'date': '2026-10-30'}, 'end': {'date': '2026-10-31'}}))

    def test_a_user_cancelled_deadline_is_not_revived(self):
        rid = deadlines.add(self.con, source='lms', ref='a', course='C', kind='due', title='Essay', due=self.day)
        self.con.execute("UPDATE deadlines SET status='cancelled' WHERE id=?", (rid,))
        deadlines.add(self.con, source='lms', ref='a', course='C', kind='due', title='Essay', due=self.day)
        self.assertEqual(self.con.execute('SELECT status FROM deadlines').fetchone()[0], 'cancelled')


if __name__ == '__main__':
    unittest.main()
