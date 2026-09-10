"""Scaffolding every collector shares.

A collector is a plain Python module that reads one source and writes facts. No
model is involved, which is the point: ingestion runs every 30 minutes and must
cost nothing. Judgment happens later, once, in a cycle.

The contract is one function taking an open connection and returning a dict of
counts. This module owns the rest: bookkeeping, failure streaks, and the rule
that a blip stays quiet while a real outage does not.

One rule every collector has to follow, because getting it wrong is silent:

    Upsert what happened. Rebuild what it means.

Source facts (a message, an event, an assignment) are immutable history — upsert
them with db.put_fact and they accumulate correctly. Derived facts (a thread is
waiting on a reply, this is the current course list) are conclusions, and a
conclusion that stops being recomputed keeps asserting yesterday's answer
forever. Call db.clear(con, source, kind) before rewriting those.
"""

from __future__ import annotations

import sys
import time
import traceback
from typing import Callable

from . import db, notify

# One failure is a blip, two in a row is a problem.
NOTIFY_AFTER_FAILURES = 2


def cursor(con, name: str) -> str | None:
    """Where this collector got to last time.

    A collector that re-reads a fixed window every run does the same work
    forever; one that remembers a position only does the work that is new. Where
    a source offers a real delta feed -- Gmail history ids, Calendar sync tokens
    -- this is where that position lives.
    """
    row = con.execute("SELECT cursor FROM collector_state WHERE collector = ?",
                      (name,)).fetchone()
    return row["cursor"] if row and row["cursor"] else None


def run(name: str, fn: Callable[[object], dict | None]) -> int:
    """Run one collector. Returns a process exit code."""
    started = time.monotonic()
    con = db.connect()

    # How bad things were before this run, so a recovery can be reported.
    row = con.execute(
        "SELECT consecutive_failures FROM collector_state WHERE collector = ?",
        (name,)).fetchone()
    was_failing = (row["consecutive_failures"] if row else 0) or 0

    try:
        result = fn(con) or {}
        counts = dict(result)
        # A collector may hand back its new position under this key.
        new_cursor = counts.pop("_cursor", None)
        con.commit()
        db.mark_collector(con, name, ok=True, cursor=new_cursor)

        # The user was told this was broken; tell them it is not. An alert with
        # no all-clear leaves someone checking a thing that fixed itself.
        if was_failing >= NOTIFY_AFTER_FAILURES:
            notify.tell(f"Herald: {name} is working again",
                        f"Recovered after {was_failing} failed runs.",
                        priority="low", tags="white_check_mark")
    except Exception:
        con.rollback()
        err = traceback.format_exc(limit=8)
        db.mark_collector(con, name, ok=False, error=err)
        streak = con.execute(
            "SELECT consecutive_failures FROM collector_state WHERE collector = ?",
            (name,)).fetchone()
        streak = streak[0] if streak else 1
        print(err, file=sys.stderr)
        if streak >= NOTIFY_AFTER_FAILURES:
            notify.tell(f"Herald: {name} is failing",
                        f"{streak} runs in a row.\n\n"
                        f"`journalctl --user -u herald-collect -n 50`",
                        priority="high", tags="rotating_light")
        return 1
    finally:
        con.close()

    elapsed = time.monotonic() - started
    summary = ", ".join(f"{v} {k}" for k, v in counts.items() if v) or "nothing new"
    print(f"{name}: {summary} ({elapsed:.1f}s)")
    return 0


def main(name: str, fn: Callable[[object], dict | None]) -> None:
    """Entry point for `python collectors/<name>.py`."""
    raise SystemExit(run(name, fn))
