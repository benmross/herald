#!/usr/bin/env python
"""Every message in their mail, ranked for the scout cycle.

This used to be a gate. It scored each message on keywords and kept only the
ones over a threshold, on the theory that a few hundred messages had to be
narrowed to a dozen before a session could look at them.

The gate came out on 9 September 2026, at the request of the person it was
filtering for: "I would like scout to always read every single one of my
emails, not run a keyword filter." That was the right call. Recall was the whole point of the thing and a keyword list cannot
have it -- the words that matter are not knowable in advance, and a message
that scored zero was never seen by anything that could tell. The App Store
Connect receipt saying an app had gone into review scored zero, and so did a
family member asking a direct question.

So this now emits a candidate for **every** message that is not from them, and
the score survives only as a ranking hint: it decides what the scout reads
first when a pass has more mail than it can hold, never what it reads at all.
`JUNK` likewise now only pushes something to the back of the queue instead of
dropping it -- a shipping notice really is worthless, but "worthless" is a
judgment and judgments belong to the cycle, not to a regex.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import collector, config, db  # noqa: E402

NAME = "opportunities"
REQUIRES = ('google',)

# How often this is worth running:
# derives from gmail facts already in the ledger
CADENCE_MINUTES = 30

# Strong signals: if one of these appears, a human would call it an opportunity.
STRONG = re.compile(
    r"\b(internship|internships|co-?op|fellowship|scholarship|hackathon|"
    r"research (?:position|assistant|opportunity|experience)|REU|"
    r"apply (?:now|today|by)|application (?:deadline|open|due)|"
    r"now (?:hiring|recruiting|accepting)|call for (?:papers|applications)|"
    r"info ?session|career fair|recruiting event|summer (?:program|analyst)|"
    r"undergraduate research|grant|stipend|paid position)\b", re.I)

# Weak signals: only count when something else corroborates.
WEAK = re.compile(
    r"\b(deadline|due (?:date|by)|opportunity|opportunities|position|opening|"
    r"cohort|applications?|selected|eligible|award|competition|workshop)\b", re.I)

# What this person in particular would want. The default is the broad shape of
# technical work; `opportunities.interests` in the config replaces it, and
# `identity/goals.md` is where the real ranking happens -- this only decides
# reading order.
_DEFAULT_INTERESTS = (r"machine learning|artificial intelligence|\bML\b|\bAI\b|"
                      r"software|computer science|\bCS\b|engineering|data|"
                      r"robotics|cyber|security|quantum|systems")
RELEVANT = re.compile(
    "(" + "|".join(config.get("opportunities.interests", []) or [_DEFAULT_INTERESTS])
    + ")", re.I)

# Senders that reliably carry opportunities. The defaults are the ones true of
# almost any inbox; `opportunities.good_senders` in the config adds whatever is
# true of this one -- a university's domain, an employer, a mailing list -- and
# is the right place for anything institution-shaped.
_DEFAULT_GOOD = (r"handshake|joinhandshake|careers|recruit|"
                 r"nsf|acm|ieee|linkedin|indeed|hackathon|mlh")
GOOD_SOURCES = re.compile(
    "(" + "|".join(filter(None, [_DEFAULT_GOOD,
                                 *(config.get("opportunities.good_senders", []) or [])]))
    + ")", re.I)

# Almost never opportunities. No longer a filter -- a match sends the message
# to the back of the reading order rather than out of the ledger.
JUNK = re.compile(
    r"(unsubscribe from all|order (?:confirmation|update)|your receipt|"
    r"shipped|delivery|statement is ready|verify your email|password reset|"
    r"security alert|two-factor)", re.I)

# Ranking floors, not gates. Nothing is excluded by score any more.
JUNK_SCORE = -1


def _score(subject: str, body: str, sender: str) -> tuple[int, list[str]]:
    blob = f"{subject}\n{body}"
    reasons, score = [], 0

    if JUNK.search(blob):
        return JUNK_SCORE, ["junk"]

    if m := STRONG.search(blob):
        score += 2
        reasons.append(f"strong:{m.group(0).lower()}")
    if m := WEAK.search(blob):
        score += 1
        reasons.append(f"weak:{m.group(0).lower()}")
    if m := RELEVANT.search(blob):
        score += 1
        reasons.append(f"relevant:{m.group(0).lower()}")
    if m := GOOD_SOURCES.search(sender or ""):
        score += 1
        reasons.append(f"source:{m.group(0).lower()}")
    return score, reasons


def collect(con) -> dict:
    rows = con.execute("""
        SELECT id, ts, title, body, data FROM facts
        WHERE source = 'gmail' AND kind = 'message'
          AND ts >= date('now', '-45 days')
    """).fetchall()

    db.clear(con, NAME, "candidate")
    kept = 0

    for r in rows:
        data = json.loads(r["data"] or "{}")
        if data.get("from_me"):
            continue
        subject = r["title"] or ""
        body = r["body"] or ""
        sender = data.get("from") or ""

        score, reasons = _score(subject, body, sender)

        db.put_fact(
            con, NAME, "candidate", external_id=f"gmail:{r['id']}", ts=r["ts"],
            title=subject, body=body,
            data={
                "from": sender,
                "score": score,
                "reasons": reasons,
                "source_fact": r["id"],
                "list_id": data.get("list_id"),
                "gmail_id": data.get("thread_id"),
            },
        )
        kept += 1

    live = con.execute(
        "SELECT COUNT(*) FROM opportunities WHERE status IN ('new','surfaced','interested')"
    ).fetchone()[0]

    # Nothing decides an opportunity is dead except the calendar.
    expired = con.execute("""
        UPDATE opportunities SET status = 'expired'
        WHERE status IN ('new', 'surfaced', 'interested')
          AND deadline IS NOT NULL AND substr(deadline, 1, 10) < date('now')
    """).rowcount

    return {"scanned": len(rows), "candidates": kept,
            "live opportunities": live, "expired": expired}



if __name__ == "__main__":
    collector.main(NAME, collect)
