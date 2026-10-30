"""Five-hour window planning, on synthetic history, without calling a model."""
import datetime as dt
import pathlib
import sqlite3
import sys
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))
from herald import anchors, think  # noqa: E402

TZ = ZoneInfo("America/New_York")
NOW = dt.datetime(2026, 10, 30, 12, 2, tzinfo=TZ)


def run(day, hour, minute=0, engine="claude", interactive=True):
    when = dt.datetime(2026, 10, 1, hour, minute, tzinfo=TZ) + dt.timedelta(days=day)
    return {"when": when, "engine": engine, "interactive": interactive,
            "minute": hour * 60 + minute}


def month(days=14):
    rows = []
    for day in range(days):
        rows += [run(day, 11, 10), run(day, 15), run(day, 23)]
        rows.append(run(day, 6, 30, interactive=False))      # the morning digest
        rows.append(run(day, 8, 15, interactive=False))
    return rows


class Plan(unittest.TestCase):
    def test_usual_start_is_the_median_first_use_past_a_late_night(self):
        rows = month() + [run(1, 1, 30)]     # 1:30 AM belongs to the night before
        self.assertEqual(anchors.usual_start(rows), (660, 14))

    def test_too_little_history_plans_nothing(self):
        self.assertIsNone(anchors.build(month(3), ("claude", "codex"), "claude"))

    def test_the_gap_avoids_scheduled_runs_and_the_providers_are_offset(self):
        plan = anchors.build(month(), ("claude", "codex"), "claude")
        first = plan["anchors"]["claude"]
        self.assertEqual(len(first), 4)
        gap = (first[0] + 1200) % 1440
        for minute in (390, 495, 670, 900, 1380):
            self.assertFalse(anchors._in(minute, gap, 240), minute)
        self.assertEqual(sorted((a + 150) % 1440 for a in first), sorted(plan["anchors"]["codex"]))

    def test_one_provider_plans_alone(self):
        plan = anchors.build(month(), ("claude",), "claude")
        self.assertEqual(list(plan["anchors"]), ["claude"])


class Windows(unittest.TestCase):
    def test_idle_is_read_per_provider(self):
        at = 1000.0
        self.assertTrue(anchors.idle("claude", {"resets_at": None, "used_percent": 0}, at))
        self.assertFalse(anchors.idle("claude", {"resets_at": at + 17990, "used_percent": 0}, at))
        self.assertTrue(anchors.idle("codex", {"resets_at": at + 18000, "used_percent": 0}, at))
        self.assertFalse(anchors.idle("codex", {"resets_at": at + 16000, "used_percent": 0}, at))
        self.assertFalse(anchors.idle("codex", {"resets_at": at + 18000, "used_percent": 1}, at))

    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.execute("CREATE TABLE runs (ts TEXT, label TEXT, engine TEXT)")
        self.con.execute("INSERT INTO runs VALUES (?,?,?)",
                         ((NOW - dt.timedelta(days=1)).isoformat(), "telegram", "claude"))
        self.con.execute("CREATE TABLE facts (source, kind, external_id, data)")
        self.plan = {"start": 660, "days": 14, "anchors": {"claude": [720], "codex": [570]}}
        self.snap = {"sampled_at": 1.0, "windows": [
            {"key": "session", "window_minutes": 300, "resets_at": None, "used_percent": 0},
            {"key": "week", "window_minutes": 10080, "resets_at": 9, "used_percent": 10}]}

    def _maintain(self, snap=None, now=NOW, ok=True):
        result = think.Result(ok=ok, error=None if ok else "no")
        with mock.patch.object(anchors, "_ping", return_value=result) as ping, \
                mock.patch.object(anchors.db, "put_fact", self._put), \
                mock.patch.object(anchors.config, "get", lambda _k, d=None: d):
            count = anchors.maintain(self.con, {"claude": snap or self.snap}, self.plan, now)
        return count, ping

    def _put(self, con, source, kind, external_id, ts, data):
        import json
        con.execute("INSERT INTO facts VALUES (?,?,?,?)", (source, kind, external_id, json.dumps(data)))

    def test_an_idle_window_opens_once_per_anchor(self):
        self.assertEqual(self._maintain()[0], 1)
        self.assertEqual(self._maintain()[0], 0)

    def test_a_running_window_or_no_anchor_spends_nothing(self):
        busy = {**self.snap, "windows": [{**self.snap["windows"][0], "resets_at": 99,
                                          "used_percent": 5}, self.snap["windows"][1]]}
        self.assertFalse(self._maintain(busy)[1].called)
        self.assertFalse(self._maintain(now=NOW + dt.timedelta(hours=3))[1].called)

    def test_a_weekly_quota_near_its_limit_is_left_alone(self):
        full = {**self.snap, "windows": [self.snap["windows"][0],
                                         {**self.snap["windows"][1], "used_percent": 95}]}
        self.assertFalse(self._maintain(full)[1].called)

    def test_nobody_using_herald_means_no_calls(self):
        self.assertFalse(self._maintain(now=NOW + dt.timedelta(days=9))[1].called)


if __name__ == "__main__":
    unittest.main()
