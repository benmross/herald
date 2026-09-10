"""One-tap approval for the things Herald must not decide alone.

The red tier in the constitution -- anything another person sees, anything
irreversible, anything that spends money -- is a rule in a prompt, and a rule
in a prompt is exactly as strong as the session reading it. That is fine for
almost everything, because almost everything red is also *asked for* in the
same conversation: the user says "reply to her", the agent replies.

It stops being fine when the action is available to a session that was not
asked. Herald reads mail, web pages and scraped listings all day, and every one
of them is text somebody else wrote; the whole security model is that fetched
content is data rather than instructions. A capability that can text a real
person is the first place where a session that talks itself past that rule does
lasting damage.

So this exists for the actions whose owner wants a machine-checkable gate
rather than a promise:

    id = approvals.request(actor="session", kind="imessage.send",
                           target="+1...", summary="Text Mum: 'running late'",
                           payload={...})
    state = approvals.wait(id, timeout=300)      # blocks
    if state == "approved":
        ...do the thing...

The process that wants the action is the one that waits and the one that acts.
The surface where the human taps yes only records the decision -- it never
carries the action out -- so a compromised session cannot approve itself by
reaching the approving code, and an approval that nobody consumes expires.
"""

from __future__ import annotations

import json
import time

from . import db

PENDING, APPROVED, DENIED, EXPIRED, DONE = (
    "pending", "approved", "denied", "expired", "done")


def request(*, actor: str, kind: str, summary: str, target: str | None = None,
            payload: dict | None = None) -> int:
    with db.session() as con:
        cur = con.execute(
            "INSERT INTO approvals (ts, actor, kind, target, summary, payload, state)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (db.now(), actor, kind, target, summary,
             json.dumps(payload or {}), PENDING))
        con.commit()
        return int(cur.lastrowid)


def get(approval_id: int) -> dict | None:
    rows = db.query("SELECT * FROM approvals WHERE id = ?", (approval_id,))
    return dict(rows[0]) if rows else None


def pending() -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT * FROM approvals WHERE state = ? ORDER BY ts", (PENDING,))]


def decide(approval_id: int, state: str, by: str) -> bool:
    """Record a decision. Only ever moves a pending row, so a second tap on an
    old notification cannot re-approve something already handled."""
    if state not in (APPROVED, DENIED):
        raise ValueError(state)
    with db.session() as con:
        cur = con.execute(
            "UPDATE approvals SET state = ?, decided_at = ?, decided_by = ?"
            " WHERE id = ? AND state = ?",
            (state, db.now(), by, approval_id, PENDING))
        con.commit()
        return cur.rowcount > 0


def mark_done(approval_id: int) -> None:
    with db.session() as con:
        con.execute("UPDATE approvals SET state = ? WHERE id = ?", (DONE, approval_id))
        con.commit()


def wait(approval_id: int, *, timeout: int = 300, poll: float = 2.0) -> str:
    """Block until decided, or time out.

    Polling a SQLite row rather than anything cleverer: the decision arrives in
    another process (the Telegram bridge), the wait is measured in minutes, and
    two seconds of latency on a message a human is thinking about is not the
    slow part.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = get(approval_id)
        if not row:
            return EXPIRED
        if row["state"] != PENDING:
            return row["state"]
        time.sleep(poll)
    with db.session() as con:
        con.execute("UPDATE approvals SET state = ? WHERE id = ? AND state = ?",
                    (EXPIRED, approval_id, PENDING))
        con.commit()
    return EXPIRED
