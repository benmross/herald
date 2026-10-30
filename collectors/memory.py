#!/usr/bin/env python
"""Keep the revision history small: drop no-op revisions and expired ones.

The runner compacts each collector's own run. This catches writers outside a
collector run (cycles, sessions) and enforces `memory.change_retention_days`.
Current records are never touched, and no model is involved.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import changes, collector, config  # noqa: E402

NAME = "memory"
REQUIRES = ()
CADENCE_MINUTES = 1440


def collect(con) -> dict:
    days = int(config.get("memory.change_retention_days", 30))
    return {"no-op revisions removed": changes.compact(con),
            "expired revisions removed": changes.prune(con, days)}


if __name__ == "__main__":
    collector.main(NAME, collect)
