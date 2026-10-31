"""Calendar times are stored local, because readers slice the clock and date
out of the string."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "collectors"))

import gcal
from herald import config, db


class LocalCalendarTimes(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(config, "tz", lambda: ZoneInfo("America/New_York"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_utc_feed_event_keeps_its_instant_and_reads_local(self):
        self.assertEqual(gcal._iso({"dateTime": "2026-10-31T16:00:00Z"}),
                         ("2026-10-31T12:00:00-04:00", False))

    def test_late_evening_utc_event_lands_on_its_local_date(self):
        self.assertEqual(gcal._iso({"dateTime": "2026-10-31T01:00:00Z"})[0][:10], "2026-10-30")

    def test_all_day_event_is_untouched(self):
        self.assertEqual(gcal._iso({"date": "2026-10-31"}), ("2026-10-31", True))

    def test_migration_rewrites_stored_times_once(self):
        spec = importlib.util.spec_from_file_location(
            "local_times", ROOT / "migrations/0010_local_calendar_times.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(config, "DB_PATH", pathlib.Path(tmp) / "facts.db"), \
                db.session() as con:
            db.put_fact(con, "gcal", "event", external_id="game", ts="2026-10-31T16:00:00Z",
                        data={"end": "2026-10-31T19:00:00Z", "location": "SECU"})
            db.put_fact(con, "gcal", "event", external_id="day", ts="2026-10-31",
                        data={"end": "2026-11-01"})
            self.assertIn("1 of 2", migration.apply(con))
            self.assertIn("0 of 2", migration.apply(con))
            row = con.execute("SELECT ts, json_extract(data,'$.end') e FROM facts "
                              "WHERE external_id='game'").fetchone()
            self.assertEqual((row["ts"], row["e"]),
                             ("2026-10-31T12:00:00-04:00", "2026-10-31T15:00:00-04:00"))


if __name__ == "__main__":
    unittest.main()
