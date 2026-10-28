#!/usr/bin/env python
"""Incremental direct mailbox access, with no changes to read flags."""
import pathlib
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "lib"))
from herald import collector, purelymail
NAME = "purelymail"
REQUIRES = ("purelymail",)
CADENCE_MINUTES = 30
collect = purelymail.collect
if __name__ == "__main__":
    collector.main(NAME, collect)
