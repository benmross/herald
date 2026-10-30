#!/usr/bin/env python
"""Keep the full-text index current. Only what changed is read again."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import collector, search  # noqa: E402

NAME = "search_index"
REQUIRES = ()
CADENCE_MINUTES = 30


def collect(con) -> dict:
    return search.refresh(con)


if __name__ == "__main__":
    collector.main(NAME, collect)
