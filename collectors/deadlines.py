#!/usr/bin/env python
"""Keep the class calendar equal to the `deadlines` table.

A collector in the scaffolding sense (a cadence, failure streaks) that writes
outward, like the campus calendar sync: every write goes through gwrite and is
reported by the next digest. Off until `deadlines.calendar` is set. The logic
is in `lib/herald/deadlines.py`; `herald deadline sync --dry-run` prints the
plan and writes nothing.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import collector, deadlines  # noqa: E402

NAME = "deadlines"
REQUIRES = ("google",)
# Feeds refresh every three hours; a date that moved should be right on the
# calendar within the same half-day, and a pass where nothing moved is a
# couple of reads.
CADENCE_MINUTES = 180


def collect(con) -> dict:
    return deadlines.sync(con, report=lambda m: print(m, file=sys.stderr))


if __name__ == "__main__":
    collector.main(NAME, collect)
