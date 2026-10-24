"""The opening_hours parser behind `herald places near --open-at`.

A wrong True sends someone to a closed door, and a wrong False hides the one
place that was open, so each form restaurants actually use in OSM is pinned.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import places  # noqa: E402

FRI_2150 = dt.datetime(2026, 10, 23, 21, 50)


class OpeningHours(unittest.TestCase):
    def test_simple_week(self):
        oh = "Mo-Th,Su 10:30-23:00; Fr,Sa 10:30-24:00"
        self.assertIs(places.is_open(oh, FRI_2150), True)
        self.assertIs(places.is_open(oh, dt.datetime(2026, 10, 24, 0, 30)), False)

    def test_past_midnight_spills_into_next_day(self):
        oh = "Fr-Sa 10:30-04:00; Su-Th 10:30-02:00"
        self.assertIs(places.is_open(oh, dt.datetime(2026, 10, 24, 3, 0)), True)

    def test_comma_between_rules(self):
        oh = "Su-We 10:00-21:00, Th-Sa 10:00-23:59"
        self.assertIs(places.is_open(oh, FRI_2150), True)
        self.assertIs(places.is_open(oh, dt.datetime(2026, 10, 25, 21, 50)), False)

    def test_day_list_is_not_split_into_rules(self):
        oh = "Mo,Su 07:00-21:00; Tu-Th 07:00-22:00; Fr-Sa 07:00-23:00"
        self.assertIs(places.is_open(oh, FRI_2150), True)
        self.assertIs(places.is_open(oh, dt.datetime(2026, 10, 26, 21, 50)), False)

    def test_off_unknown_and_always(self):
        self.assertIs(places.is_open("Mo-Fr 09:00-17:00; Sa,Su off",
                                     dt.datetime(2026, 10, 24, 12, 0)), False)
        self.assertIsNone(places.is_open("Jan-Mar Mo-Fr 09:00-17:00", FRI_2150))
        self.assertIsNone(places.is_open(None, FRI_2150))
        self.assertIs(places.is_open("24/7", FRI_2150), True)


if __name__ == "__main__":
    unittest.main()
