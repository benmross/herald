#!/usr/bin/env python3
"""Read CLI subscription quotas and send reminders without inference."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))
from herald import collector, usage

NAME = "usage"
REQUIRES = ()
CADENCE_MINUTES = 30


def collect(con):
    return usage.collect(con)


if __name__ == "__main__":
    collector.main(NAME, collect)
