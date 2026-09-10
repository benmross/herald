"""The calendar sync planner: the pure diff, with no calendar and no network.

Every case here is a bug someone had first -- a duplicate created because a
hand-added event was not adopted, a person's own edits overwritten, a whole
calendar greyed because the snapshot behind it was half-migrated. The planner
is pure precisely so these can be cheap.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import calsync, gwrite  # noqa: E402


class CalendarPlanner(unittest.TestCase):
    def setUp(self):
        self.plan = calsync.plan
        self.today = dt.date(2026, 9, 9)

    def _want(self, key, title, start="2026-09-10T18:00:00-04:00", aliases=()):
        body = gwrite.build_event(summary=title, start=start, end=None, all_day=False,
                                  location="Stamp", description="d", color_id="6",
                                  sync_key=key)
        body["_aliases"] = list(aliases)
        body["_source"] = key.split(":")[0]
        return body

    def _live(self, key, title, start="2026-09-10T18:00:00-04:00", **over):
        body = gwrite.build_event(summary=title, start=start, end=None, all_day=False,
                                  location="Stamp", description="d", color_id="6",
                                  sync_key=key or "x")
        props = ({"syncKey": key,
                  "syncHash": body["extendedProperties"]["private"]["syncHash"]}
                 if key else {})
        e = gwrite.summarize({"id": f"id-{title}", "summary": title,
                              "start": body["start"], "end": body["end"],
                              "location": "Stamp", "description": "d", "colorId": "6",
                              "extendedProperties": {"private": props}})
        e.update(over)
        return e

    def test_create_when_missing(self):
        ops = self.plan([], {"feed:1": self._want("feed:1", "Fair")}, set(),
                        {"feed"}, self.today)
        self.assertEqual([o[0] for o in ops], ["create"])

    def test_unchanged_when_identical(self):
        live = [self._live("feed:1", "Fair")]
        ops = self.plan(live, {"feed:1": self._want("feed:1", "Fair")}, set(),
                        {"feed"}, self.today)
        self.assertEqual([o[0] for o in ops], ["unchanged"])

    def test_adopts_unowned_by_title_and_start(self):
        live = [self._live("", "Fair")]
        ops = self.plan(live, {"feed:1": self._want("feed:1", "Fair")}, set(),
                        {"feed"}, self.today)
        self.assertEqual(ops[0][0], "stamp")
        self.assertEqual(ops[0][4], "adopt")

    def test_rekeys_via_alias_and_does_not_cancel_old_key(self):
        live = [self._live("other:fair", "Fair")]
        ops = self.plan(live, {"feed:1": self._want("feed:1", "Fair",
                                                        aliases=["other:fair"])},
                        set(), {"feed", "other"}, self.today)
        self.assertEqual(ops[0][0], "stamp")
        self.assertEqual(ops[0][4], "rekey")
        self.assertEqual(len(ops), 1)

    def test_hand_edited_is_merged_not_overwritten(self):
        live = [self._live("feed:1", "Fair", user_modified=True)]
        ops = self.plan(live, {"feed:1": self._want("feed:1", "Fair (moved)")},
                        set(), {"feed"}, self.today)
        self.assertEqual(ops[0][0], "merge")

    def test_vanished_from_healthy_source_is_cancelled(self):
        live = [self._live("feed:9", "Gone")]
        ops = self.plan(live, {}, set(), {"feed"}, self.today)
        self.assertEqual([o[0] for o in ops], ["cancel"])

    def test_vanished_from_tripped_source_is_left_alone(self):
        live = [self._live("partial:9", "Gone")]
        ops = self.plan(live, {}, set(), {"feed"}, self.today)
        self.assertEqual(ops, [])

    def test_email_keys_are_never_cancelled(self):
        live = [self._live("email:career-fair", "Career Fair")]
        ops = self.plan(live, {}, set(), {"feed"}, self.today)
        self.assertEqual(ops, [])

    def test_today_vanished_is_not_cancelled(self):
        live = [self._live("feed:9", "Today", start="2026-09-09T09:00:00-04:00")]
        ops = self.plan(live, {}, set(), {"feed"}, self.today)
        self.assertEqual(ops, [])

    def test_an_excluded_event_already_on_the_calendar_is_purged(self):
        # Until 9 Sep 2026 `ignored` was a set of keys to leave alone, so the
        # religious and greek-life filters only ever applied to events not yet
        # written. asked for both categories gone now as well as in
        # future, so it became a dict of key -> why and the op became a purge.
        # This test still asserted the old "do nothing" contract against the
        # old set, and had been failing since that commit.
        live = [self._live("feed:7", "Unwanted Event")]
        ops = self.plan(live, {}, set(), {"feed"}, self.today,
                        {"feed:7": "religious"})
        self.assertEqual([(o[0], o[1], o[4]) for o in ops],
                         [("purge", "feed:7", "religious")])

    def test_an_event_nobody_excluded_is_not_purged(self):
        live = [self._live("feed:7", "Career Fair")]
        ops = self.plan(live, {"feed:7": self._want("feed:7", "Career Fair")},
                        set(), {"feed"}, self.today, {})
        self.assertNotIn("purge", [o[0] for o in ops])

    def test_explicit_cancellation_greys_even_today(self):
        live = [self._live("feed:9", "Today", start="2026-09-09T09:00:00-04:00")]
        ops = self.plan(live, {}, {"feed:9"}, set(), self.today)
        self.assertEqual([o[0] for o in ops], ["cancel"])



if __name__ == "__main__":
    unittest.main()
