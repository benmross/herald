#!/usr/bin/env python
"""Expire revision history under memory.change_retention_days.

Readers inside a net-zero interval still need its transitions. Current records
are never touched, and no model is involved.
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
    return {"expired revisions removed": changes.prune(con, days)}


if __name__ == "__main__":
    collector.main(NAME, collect)
