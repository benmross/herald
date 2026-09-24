"""Obligations: the open loops, and only those.

The `commitments` table is the obligation list. What changed on 23 September
2026, when the user reviewed it for the first time and found he had never been
shown most of it:

- **Only open loops belong here**: something owed to a person, an unanswered
  question, a promise, a task, a security problem. A quiz, an exam or a due date
  is a date, and dates live on the calendar (`deadlines.py`).
- **Nothing is tracked silently.** Every new row is put in front of the user
  once, with Keep / Done / Drop, the moment it is added. About a quarter of the
  77 rows he reviewed were wrong (not his, long finished, or placeholders for
  dates nobody had published), and one tap at the time would have caught each.
- **Undated rows go stale.** One nobody has confirmed in `stale_days` is asked
  about again rather than sitting for two months.
- **Security problems are pushed, not summarised.** A leaked, still-live secret
  was mentioned twice as a clause inside a morning digest and never on its own.
- **Closing on evidence is said out loud.** `close()` tells the user what was
  closed and why, so an automatic close can be caught if it was wrong.

Everything here is deterministic. The model decides *that* something is an
obligation (the dawn cycle, a conversation); this module makes sure the user
hears about it and gets the last word.
"""

from __future__ import annotations

import datetime as dt

from . import config, db, notify

KINDS = ("owed", "promised", "unanswered", "task", "security")
CALLBACK = "oblig"

# At most this many messages in one sweep, so a backlog written some other
# way arrives as a handful rather than a flood. The rest go next pass.
MAX_ANNOUNCE_PER_PASS = 5
MAX_ASK_PER_PASS = 2


def _stale_days() -> int:
    return int(config.get("obligations.stale_days", 14) or 14)


def _row(con, oid: int) -> dict | None:
    r = con.execute("SELECT * FROM commitments WHERE id=?", (oid,)).fetchone()
    return dict(r) if r else None


def _when(due: str | None) -> str:
    if not due:
        return ""
    try:
        d = dt.date.fromisoformat(due[:10])
    except ValueError:
        return due
    t = due[11:16] if len(due) > 10 else ""
    return d.strftime("%a %-d %b") + (f" {t}" if t and t != "00:00" else "")


def _keyboard(oid: int) -> list[list[tuple[str, str]]]:
    return [[("Keep", f"{CALLBACK}:{oid}:keep"), ("Done", f"{CALLBACK}:{oid}:done"),
             ("Drop", f"{CALLBACK}:{oid}:drop")]]


def _card(row: dict, head: str) -> str:
    due = _when(row["due"])
    lines = [f"**{head} #{row['id']}:** {row['text']}"]
    if due:
        lines.append(f"Due {due}.")
    note = (row["notes"] or "").strip().splitlines()
    if note:
        lines.append(note[0][:300])
    return "\n".join(lines)


def announce(con, row: dict) -> bool:
    """Show a newly tracked obligation once. A security one is a push in its
    own right, which is the point: it never waits for a digest."""
    head = "Security" if row["kind"] == "security" else "Now tracking"
    ok = notify.buttons(_card(row, head), _keyboard(row["id"]))
    if ok:
        con.execute("UPDATE commitments SET announced_at=? WHERE id=?", (db.now(), row["id"]))
    return ok


def ask(con, row: dict) -> bool:
    """The "still real?" check for an undated row nobody has confirmed lately."""
    since = (row["confirmed_at"] or row["announced_at"] or row["created_at"] or "")[:10]
    ok = notify.buttons(_card(row, f"Still real? (tracked since {since})"),
                        _keyboard(row["id"]))
    if ok:
        con.execute("UPDATE commitments SET asked_at=? WHERE id=?", (db.now(), row["id"]))
    return ok


def add(con, *, text: str, kind: str, due: str | None = None,
        notes: str | None = None, source_fact: int | None = None,
        tell: bool = True) -> int:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}; a dated course item is a"
                         " deadline (`herald deadline add`), not an obligation")
    cur = con.execute(
        "INSERT INTO commitments (created_at, text, kind, due, source_fact, status, notes)"
        " VALUES (?,?,?,?,?,'open',?)", (db.now(), text, kind, due, source_fact, notes))
    oid = cur.lastrowid
    con.commit()
    if tell:
        announce(con, _row(con, oid))
    return oid


def close(con, oid: int, status: str, why: str, *, tell: bool = True) -> dict | None:
    """Close one row. `tell` says so on Telegram, which is what makes a close
    Herald did on evidence something the user can catch if it was wrong."""
    if status not in ("done", "dropped"):
        raise ValueError("status is done or dropped")
    row = _row(con, oid)
    if not row or row["status"] != "open":
        return None
    now = db.now()
    con.execute("UPDATE commitments SET status=?, closed_at=?,"
                " notes=coalesce(notes,'') || ? WHERE id=?",
                (status, now, f"\n[{now[:10]}] {status}: {why}", oid))
    con.commit()
    if tell:
        verb = "Closed" if status == "done" else "Dropped"
        notify.telegram(f"**{verb} #{oid}:** {row['text']}\n{why}")
    return row


def on_callback(con, data: str) -> tuple[str, str]:
    """A Keep/Done/Drop tap. Returns (toast, line to append to the message)."""
    try:
        _, sid, action = data.split(":")
        oid = int(sid)
    except ValueError:
        return "Not understood.", ""
    row = _row(con, oid)
    if not row:
        return "That one no longer exists.", ""
    if row["status"] != "open":
        return f"Already {row['status']}.", f"(already {row['status']})"
    now = db.now()
    if action == "keep":
        con.execute("UPDATE commitments SET confirmed_at=? WHERE id=?", (now, oid))
        return "Kept.", "Kept."
    if action in ("done", "drop"):
        status = "done" if action == "done" else "dropped"
        con.execute("UPDATE commitments SET status=?, closed_at=?,"
                    " notes=coalesce(notes,'') || ? WHERE id=?",
                    (status, now, f"\n[{now[:10]}] {status}: the user tapped it", oid))
        return ("Marked done." if status == "done" else "Dropped."), \
               ("Done." if status == "done" else "Dropped.")
    return "Not understood.", ""


def sweep(con) -> dict:
    """Announce what has never been shown; ask about what has gone quiet."""
    counts = {"announced": 0, "asked": 0}
    fresh = con.execute(
        "SELECT * FROM commitments WHERE status='open' AND announced_at IS NULL"
        " ORDER BY id LIMIT ?", (MAX_ANNOUNCE_PER_PASS,)).fetchall()
    for r in fresh:
        counts["announced"] += announce(con, dict(r))
    cutoff = (dt.datetime.now(config.tz()) - dt.timedelta(days=_stale_days())).isoformat()
    stale = con.execute(
        "SELECT * FROM commitments WHERE status='open' AND due IS NULL"
        " AND announced_at IS NOT NULL"
        " AND coalesce(confirmed_at, announced_at) < ?"
        " AND (asked_at IS NULL OR asked_at < ?)"
        " ORDER BY coalesce(confirmed_at, announced_at) LIMIT ?",
        (cutoff, cutoff, MAX_ASK_PER_PASS)).fetchall()
    for r in stale:
        counts["asked"] += ask(con, dict(r))
    con.commit()
    return {k: v for k, v in counts.items() if v} or {"nothing new": 0}


def render(con, *, deadline_days: int = 7) -> str:
    """The /obligations reply: every open loop, then what is due this week."""
    from . import deadlines  # local: deadlines pulls in the Google client

    rows = [dict(r) for r in con.execute(
        "SELECT * FROM commitments WHERE status='open'"
        " ORDER BY due IS NULL, replace(due, ' ', 'T'), id")]
    out = [f"**Obligations** ({len(rows)} open)"]
    for r in rows:
        due = _when(r["due"])
        flag = "⚠️ " if r["kind"] == "security" else ""
        out.append(f"#{r['id']} {flag}{r['text']}" + (f" ({due})" if due else ""))
    if not rows:
        out.append("None open.")
    soon = deadlines.upcoming(con, deadline_days)
    if soon:
        out.append(f"\n**On the calendar, next {deadline_days} days**")
        for d in soon:
            out.append(f"{_when(d['due'])}: {d['title']}"
                       + (f" ({d['kind']})" if d["kind"] in ("quiz", "test") else ""))
    out.append("\nTell me in chat to close, drop or change any of these.")
    return "\n".join(out)
