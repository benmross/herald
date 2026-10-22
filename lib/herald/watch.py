"""Watching a page until the thing it is about opens.

NVIDIA's Ignite programme for first- and second-year students took
applications from 5 to 18 October 2026. Herald had flagged it on 7 September,
and its opportunities row said to check again for a live posting. A check on
5 October, the day it opened, called it not open, and nothing checked again.
The user heard about it from a friend four days after it closed. The plan to
recheck existed only as a sentence in a notes column, and a sentence does not
run.

A watch is that sentence made into a schedule. An opportunity row gets a
`watch_url`, and optionally `watch_for`, a regular expression for what the
page will say once it is open ("2027 Ignite", "Apply"). The `watch`
collector renders each watched page in a headless browser a few times a day:

- With `watch_for`: the first time it matches, the user is told at once.
  Programmes like this open for two weeks, so the morning digest is too
  slow and a deadline-style push is right.
- Without it: a change in the page's text (digits ignored) is recorded as a fact for the next
  scout pass to judge. Pages change for many reasons, so this does not push.

A watch stops on its own once its row is applied, passed, expired, rejected
or accepted.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re

from . import db

DONE = ("applied", "passed", "expired", "rejected", "accepted")


def _norm(text: str) -> str:
    # Dates, counts and tokens churn on every load; strip digits-only runs so
    # "Posted 3 days ago" does not read as a change.
    text = re.sub(r"\d+", "#", text)
    return re.sub(r"\s+", " ", text).strip()


def due(con) -> list:
    marks = ",".join("?" * len(DONE))
    return con.execute(f"""
        SELECT * FROM opportunities
        WHERE watch_url IS NOT NULL AND watch_url != ''
          AND status NOT IN ({marks})
        ORDER BY COALESCE(watch_checked, '')""", DONE).fetchall()


def check(con, row, fetch) -> dict:
    """Check one watched row. `fetch(url) -> (final_url, text)`.

    Returns what happened: {"id", "event": hit|changed|same|first|error, ...}.
    Writes the row's watch columns and, on a hit or change, a `watch` fact.
    """
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    out = {"id": row["id"], "title": row["title"], "org": row["org"]}
    try:
        final, text = fetch(row["watch_url"])
    except Exception as e:
        con.execute("UPDATE opportunities SET watch_checked=?, watch_error=? WHERE id=?",
                    (now, str(e)[:300], row["id"]))
        return {**out, "event": "error", "error": str(e)[:300]}

    digest = hashlib.sha256(_norm(text).encode()).hexdigest()[:16]
    excerpt = None
    if row["watch_for"]:
        m = re.search(row["watch_for"], text, re.I)
        if m:
            a, b = max(0, m.start() - 120), min(len(text), m.end() + 160)
            excerpt = re.sub(r"\s+", " ", text[a:b]).strip()

    was_hit = bool(row["watch_hit"])
    if excerpt and not was_hit:
        event = "hit"
    elif row["watch_hash"] is None:
        event = "first"
    elif row["watch_for"]:
        # With a pattern, only the pattern matters. Search pages reshuffle
        # unrelated results on every load (LinkedIn did on the first run), and
        # reporting each reshuffle would bury the one change that counts.
        event = "same"
    elif digest != row["watch_hash"]:
        event = "changed"
    else:
        event = "same"

    con.execute("""UPDATE opportunities SET watch_checked=?, watch_hash=?,
                   watch_hit=COALESCE(?, watch_hit), watch_error=NULL WHERE id=?""",
                (now, digest, excerpt, row["id"]))
    if event in ("hit", "changed"):
        db.put_fact(con, "watch", event, external_id=f"{row['id']}:{digest}", ts=now,
                    title=f"{row['org'] or ''}: {row['title']}".strip(": "),
                    body=excerpt or text[:1500],
                    data={"opportunity": row["id"], "url": final,
                          "watch_url": row["watch_url"], "watch_for": row["watch_for"]})
    return {**out, "event": event, "url": final, "excerpt": excerpt}
