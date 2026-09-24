#!/usr/bin/env python
"""Tell the user about every obligation Herald starts tracking, and ask again
about the ones that have gone quiet.

Deterministic and free: a Telegram message with keep/done/drop buttons, sent
by `lib/herald/obligations.py`. Nothing here judges anything.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import collector, obligations  # noqa: E402

NAME = "obligations"
REQUIRES = ("telegram",)
# Rows are usually announced the moment they are added (`herald obligation
# add`); this pass catches anything written another way, and the stale check
# only needs to run a few times a day.
CADENCE_MINUTES = 60


def collect(con) -> dict:
    return obligations.sweep(con)


if __name__ == "__main__":
    collector.main(NAME, collect)
