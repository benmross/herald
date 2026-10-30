"""A startup card must not misstate personal commitments or omit fresh warnings."""
from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3
import sys
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'lib'))
from cycles import _snapshot
from herald import db


class FrozenClock(dt.datetime):
    # UTC is already the next day. Calendar and due-date bounds must stay local.
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 30, 23, 30, tzinfo=ZoneInfo('America/New_York')).astimezone(tz)


class Orientation(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(':memory:')
        self.con.row_factory = sqlite3.Row
        self.con.executescript(db.SCHEMA)
        self.addCleanup(self.con.close)
        for patch in (mock.patch.object(_snapshot.dt, 'datetime', FrozenClock),
                      mock.patch.object(_snapshot.config, 'tz', return_value=ZoneInfo('America/New_York')),
                      mock.patch.object(_snapshot.changes, 'health', return_value=[]),
                      mock.patch.object(_snapshot, '_disputed_dates', return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)

    def event(self, key, start, end=None, role='mine', **extra):
        db.put_fact(self.con, 'gcal', 'event', external_id=key, title=key, ts=start,
                    data={'end': end, 'role': role, **extra})

    def test_remaining_calendar_uses_local_date_and_keeps_ongoing_events(self):
        self.event('ended', '2026-10-30T22:00:00-04:00', '2026-10-30T23:00:00-04:00')
        self.event('ongoing', '2026-10-30T23:00:00-04:00', '2026-10-30T23:45:00-04:00')
        self.event('later', '2026-10-30T23:50:00-04:00', '2026-10-31T00:30:00-04:00')
        self.event('tomorrow', '2026-10-31T01:00:00-04:00', '2026-10-31T02:00:00-04:00')
        card = _snapshot.orientation(self.con)
        self.assertIn('ongoing', card)
        self.assertIn('later', card)
        self.assertNotIn('ended', card)
        self.assertNotIn('tomorrow', card)

    def test_cancelled_and_nonpersonal_events_are_excluded(self):
        for role in ('feed', 'ignored', 'other-person', 'reference'):
            self.event(role, '2026-10-30', role=role, all_day=True)
        self.event('cancelled status', '2026-10-30', status='cancelled', all_day=True)
        self.event('CANCELLED - old deadline', '2026-10-30', all_day=True)
        self.event('personal all day', '2026-10-30', all_day=True)
        card = _snapshot.orientation(self.con)
        self.assertIn('personal all day', card)
        for title in ('feed', 'ignored', 'other-person', 'reference', 'cancelled status', 'old deadline'):
            self.assertNotIn(title, card)

    def test_overdue_obligations_do_not_displace_upcoming_dates(self):
        for i in range(12):
            self.con.execute("INSERT INTO commitments(text,due,status,created_at) VALUES (?,?,?,?)",
                             (f'long overdue task {i}', '2026-10-15', 'open', '2026-10-01'))
        self.con.execute("INSERT INTO deadlines(title,kind,due,status,source,created_at,updated_at) "
                         "VALUES (?,?,?,?,?,?,?)",
                         ('due locally today', 'due', '2026-10-30', 'live', 'manual',
                          '2026-10-01', '2026-10-01'))
        card = _snapshot.orientation(self.con)
        self.assertIn('due locally today', card)
        self.assertIn('12 (12 overdue)', card)
        self.assertNotIn('long overdue task', card)

    def test_disputed_dates_and_incomplete_coverage_survive_compaction(self):
        with mock.patch.object(_snapshot, '_disputed_dates', return_value=['- verify changed assignment']), \
                mock.patch.object(_snapshot.changes, 'health', return_value=['stale']), \
                mock.patch.object(_snapshot.changes, 'health_lines', return_value=['Mail coverage incomplete']):
            card = _snapshot.orientation(self.con)
        self.assertIn('verify changed assignment', card)
        self.assertIn('Mail coverage incomplete', card)

    def test_calendar_overflow_is_explicit(self):
        for i in range(_snapshot.ORIENTATION_LIMIT + 2):
            self.event(f'appointment {i}', '2026-10-30', all_day=True)
        self.assertIn('2 more;', _snapshot.orientation(self.con))
