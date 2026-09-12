"""The ledger's structured half.

One wide `facts` table with a JSON payload carries every source, because the
sources are heterogeneous and a table per source would mean a migration per
source. Views on top give each source a comfortable shape.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import sqlite3
from typing import Any, Iterable

from . import config

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Everything ingested, from every source.
CREATE TABLE IF NOT EXISTS facts (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,          -- gmail, gcal, and whatever else is connected
    kind         TEXT NOT NULL,          -- email, event, assignment, location, ...
    external_id  TEXT,                   -- the source's own id, for idempotent upsert
    ts           TEXT,                   -- ISO8601, when the thing happened
    title        TEXT,
    body         TEXT,
    data         TEXT,                   -- JSON, source-specific
    ingested_at  TEXT NOT NULL,
    UNIQUE (source, kind, external_id)
);
CREATE INDEX IF NOT EXISTS idx_facts_ts     ON facts (ts DESC);
CREATE INDEX IF NOT EXISTS idx_facts_lookup ON facts (source, kind, ts DESC);

-- Every agent invocation, so "what is eating my quota" is a query.
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    label         TEXT NOT NULL,         -- cycle:dawn, ask, collector:gmail, ...
    engine        TEXT NOT NULL,         -- 'claude'; there is one engine
    model         TEXT,
    session_id    TEXT,
    duration_ms   INTEGER,
    cost_usd      REAL,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cache_read    INTEGER,
    cache_write   INTEGER,
    context_tokens INTEGER,             -- size of the *last* prompt in the run
                                         -- (input + cache_read + cache_write of
                                         -- its final message, not summed across
                                         -- the run) -- "how big is the
                                         -- conversation right now", distinct
                                         -- from the summed columns above which
                                         -- answer "what did this run cost"
    num_turns     INTEGER,
    exit_code     INTEGER,
    fell_back     INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    -- Where the wall clock went, which is a different question from what the
    -- run cost. Measured 12 Sep 2026 across 108 transcripts: model time was
    -- 68% of all elapsed time, and a turn averaged 21 model round trips. So
    -- latency is round trips multiplied by per-round-trip model time, and
    -- nothing above can see either. These four can.
    startup_ms    INTEGER,              -- launch until the CLI's init event
    model_ms      INTEGER,              -- summed thinking and generating
    tool_ms       INTEGER,              -- summed tool execution
    round_trips   INTEGER               -- assistant events; the latency driver
);
CREATE INDEX IF NOT EXISTS idx_runs_ts ON runs (ts DESC);

-- One row per phase of a run, so "which tool ate the turn" is a query rather
-- than an afternoon with the transcripts. Written by think.py from the
-- stream-json events it already parses, so it costs no tokens and no extra
-- round trip.
CREATE TABLE IF NOT EXISTS run_phases (
    id      INTEGER PRIMARY KEY,
    run_id  INTEGER NOT NULL,
    seq     INTEGER NOT NULL,           -- order within the run
    kind    TEXT NOT NULL,              -- 'startup' | 'model' | 'tool'
    name    TEXT,                       -- tool name, for kind='tool'
    ms      INTEGER NOT NULL,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_phases_run ON run_phases (run_id, seq);
CREATE INDEX IF NOT EXISTS idx_run_phases_kind ON run_phases (kind, name);

-- Open loops. The thing that makes Herald feel like it is paying attention.
CREATE TABLE IF NOT EXISTS commitments (
    id          INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL,
    text        TEXT NOT NULL,
    kind        TEXT,                    -- owed | promised | deadline | unanswered
    due         TEXT,
    source_fact INTEGER REFERENCES facts (id) ON DELETE SET NULL,
    status      TEXT NOT NULL DEFAULT 'open',   -- open | done | dropped
    closed_at   TEXT,
    notes       TEXT
);
CREATE INDEX IF NOT EXISTS idx_commit_open ON commitments (status, due);

-- Things the user could apply for, go to, or ask about. Separate from facts because
-- an opportunity has a lifecycle -- found, surfaced, acted on, expired -- and a
-- fact does not. The whole point is that something surfaced once and forgotten
-- is the failure this table exists to prevent.
CREATE TABLE IF NOT EXISTS opportunities (
    id          INTEGER PRIMARY KEY,
    found_at    TEXT NOT NULL,
    title       TEXT NOT NULL,
    org         TEXT,
    kind        TEXT,                    -- internship | research | scholarship
                                         -- | hackathon | event | job | other
    deadline    TEXT,
    url         TEXT,
    source_fact INTEGER REFERENCES facts (id) ON DELETE SET NULL,
    fit         TEXT,                    -- why this one, for this person
    score       INTEGER,                 -- 1 (marginal) .. 5 (drop everything)
    status      TEXT NOT NULL DEFAULT 'new',
                                         -- new | surfaced | interested
                                         -- | applied | passed | expired
    notified_at TEXT,
    notes       TEXT,
    UNIQUE (title, org)
);
CREATE INDEX IF NOT EXISTS idx_opp_live ON opportunities (status, deadline);

-- What the digest has already told the user.
--
-- A briefing is supposed to tell them what the user does not know. Without this, every
-- morning re-reports the same exam three weeks running because it is still on
-- the calendar -- which is how a digest stops being read. A thing is new until
-- it has been in a digest once; after that it is context, and only earns a line
-- again if it changed or is about to happen.
CREATE TABLE IF NOT EXISTS reported (
    ref            TEXT PRIMARY KEY,   -- "gcal:<id>", "commitment:12", "opp:7"
    kind           TEXT NOT NULL,
    first_reported TEXT NOT NULL,
    last_reported  TEXT NOT NULL,
    times          INTEGER NOT NULL DEFAULT 1
);

-- Per-collector bookkeeping: cursors and failure streaks.
CREATE TABLE IF NOT EXISTS collector_state (
    collector            TEXT PRIMARY KEY,
    last_run             TEXT,
    last_ok              TEXT,
    cursor               TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT
);

-- What Herald sent you, so it can tell whether it is being noisy.
CREATE TABLE IF NOT EXISTS notifications (
    id       INTEGER PRIMARY KEY,
    ts       TEXT NOT NULL,
    channel  TEXT NOT NULL,
    priority TEXT,
    title    TEXT,
    body     TEXT,
    ok       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_notif_ts ON notifications (ts DESC);

-- Every write Herald makes outside its own ledger: a calendar event, a task,
-- a label, a draft. CLAUDE.md's amber tier says such an action is "never
-- silent -- every amber action appears in the journal and the next digest",
-- and a rule like that is only real if the writes are recorded somewhere the
-- digest can read. `lib/herald/gwrite.py` is the only module that writes to
-- Google and it logs here on every call; the dawn snapshot lists whatever has
-- accumulated since the last digest. `reported_at` is set once a digest has
-- carried it, so the same action is never reported twice.
CREATE TABLE IF NOT EXISTS actions (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,          -- collector:campus_calendar, cycle:scout, telegram, ...
    tier        TEXT NOT NULL,          -- green | amber | red
    kind        TEXT NOT NULL,          -- calendar.create, task.create, gmail.label, ...
    target      TEXT,                   -- the thing acted on: calendar name, list, message id
    summary     TEXT NOT NULL,          -- one line a human can read
    ref         TEXT,                   -- id of what was written, for undo
    reported_at TEXT,
    -- The tap that authorised a red action. NULL for green and amber, and
    -- gwrite/red.py refuse to write a red row without one, so a red action in
    -- this table either points at an approval the user granted or does not
    -- exist.
    approval_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_actions_unreported ON actions (reported_at, ts);

-- Red-tier actions waiting on a human. CLAUDE.md's red tier -- anything
-- another person sees, anything irreversible -- is enforced by a rule in a
-- prompt, which is exactly as strong as the session reading it. This table is
-- the mechanism for the cases where that is not strong enough: the actor
-- writes what it wants to do, a person taps yes or no somewhere Herald cannot
-- reach on its own, and only then does the actor carry it out. The waiting
-- process is the one that acts; the surface that collects the answer only
-- records it, so nothing can approve itself.
CREATE TABLE IF NOT EXISTS approvals (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,          -- who is asking: session, cycle, tool
    kind        TEXT NOT NULL,          -- imessage.send, mail.send, ...
    target      TEXT,                   -- who or what it acts on
    summary     TEXT NOT NULL,          -- exactly what will happen, in one line
    payload     TEXT,                   -- JSON the actor needs to carry it out
    state       TEXT NOT NULL,          -- pending | approved | denied | expired | done
    decided_at  TEXT,
    decided_by  TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_pending ON approvals (state, ts);
"""


def now() -> str:
    return dt.datetime.now(config.tz()).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    config.ensure_dirs()
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    _migrate(con)
    return con


# Always reach for this, never bare `with connect() as con:`. A `with` block on
# a sqlite3.Connection commits the transaction -- it does *not* close the
# connection, and the connection sits in a reference cycle (its statement
# cache), so refcounting never reclaims it either; only a sporadic gen-2 GC
# pass does. In a short-lived collector that is invisible. In a daemon it is
# fatal: herald-telegram checkpoints once per 50s poll, leaked one connection
# (two fds, counting the WAL) each time, and hit the 1024-fd limit after ~7
# hours -- going deaf on Telegram while systemd still showed it green.
# 8 Sep 2026.
@contextlib.contextmanager
def session():
    """A connection that commits on a clean exit and always closes."""
    con = connect()
    try:
        with con:
            yield con
    finally:
        con.close()


# `CREATE TABLE IF NOT EXISTS` above only ever creates a table once -- it does
# nothing for a column added later to a table that already exists on disk.
# This is the first time that has come up (`runs.context_tokens`, 7 Sep 2026),
# so the pattern going forward: add the column to SCHEMA for a fresh database,
# and add one `_add_column_if_missing` call here for every database that
# already exists. SQLite has no "ADD COLUMN IF NOT EXISTS", so check first.
def _add_column_if_missing(con: sqlite3.Connection, table: str, column: str,
                           coltype: str) -> None:
    cols = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def _migrate(con: sqlite3.Connection) -> None:
    _add_column_if_missing(con, "runs", "context_tokens", "INTEGER")
    for col in ("startup_ms", "model_ms", "tool_ms", "round_trips"):
        _add_column_if_missing(con, "runs", col, "INTEGER")
    _add_column_if_missing(con, "actions", "approval_id", "INTEGER")
    con.commit()


def _one(cur: sqlite3.Cursor):
    """Drain and close a RETURNING cursor, yielding its single value.

    sqlite3 treats a RETURNING cursor as a statement still in progress until it
    is exhausted. Committing before that raises "SQL statements in progress",
    which is a confusing way to discover you forgot to drain it.
    """
    rows = cur.fetchall()
    cur.close()
    return rows[0][0] if rows else None


def put_fact(con: sqlite3.Connection, source: str, kind: str, *,
             external_id: str | None = None, ts: str | None = None,
             title: str | None = None, body: str | None = None,
             data: Any = None) -> int:
    """Insert or update one fact. Returns its row id.

    Upserting on (source, kind, external_id) means a collector can re-read its
    whole window on every run without creating duplicates, which is much simpler
    to get right than incremental cursors.
    """
    payload = json.dumps(data, default=str) if data is not None else None
    cur = con.execute(
        """
        INSERT INTO facts (source, kind, external_id, ts, title, body, data, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source, kind, external_id) DO UPDATE SET
            ts = excluded.ts, title = excluded.title, body = excluded.body,
            data = excluded.data, ingested_at = excluded.ingested_at
        RETURNING id
        """,
        (source, kind, external_id, ts, title, body, payload, now()),
    )
    return _one(cur)


def record_action(con: sqlite3.Connection, *, actor: str, tier: str, kind: str,
                  summary: str, target: str | None = None,
                  ref: str | None = None,
                  approval_id: int | None = None) -> int:
    """Log one write that happened outside the ledger. See the actions table.

    Refuses rather than records a bad row. The tier used to be a free-text label
    nobody checked, so a caller could file a red action as green and the digest
    would report it as routine. A red row must now name the approval that
    allowed it, and that approval must actually have been granted.
    """
    from . import policy  # local: policy imports nothing from here, keep it so

    policy.validate_tier(tier)
    if tier == policy.RED:
        if approval_id is None:
            raise PermissionError(
                f"refusing to record red action {kind!r} without an approval id")
        row = con.execute("SELECT state, kind FROM approvals WHERE id = ?",
                          (approval_id,)).fetchone()
        if row is None or row[0] not in ("approved", "done"):
            raise PermissionError(
                f"approval {approval_id} is not granted "
                f"({row[0] if row else 'missing'}); red action {kind!r} not recorded")
    cur = con.execute(
        """
        INSERT INTO actions (ts, actor, tier, kind, target, summary, ref, approval_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
        """,
        (now(), actor, tier, kind, target, summary, ref, approval_id),
    )
    return _one(cur)


def unreported_actions(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Amber actions no digest has carried yet, oldest first."""
    return con.execute(
        "SELECT * FROM actions WHERE reported_at IS NULL ORDER BY ts"
    ).fetchall()


def mark_actions_reported(con: sqlite3.Connection, ids) -> None:
    ids = list(ids)
    if not ids:
        return
    con.executemany("UPDATE actions SET reported_at = ? WHERE id = ?",
                    [(now(), i) for i in ids])
    con.commit()


def already_reported(con: sqlite3.Connection) -> set[str]:
    """Everything the digest has mentioned at least once."""
    return {r[0] for r in con.execute("SELECT ref FROM reported")}


def mark_reported(con: sqlite3.Connection, refs) -> None:
    now_ = now()
    con.executemany("""
        INSERT INTO reported (ref, kind, first_reported, last_reported, times)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT (ref) DO UPDATE SET
            last_reported = excluded.last_reported, times = reported.times + 1
    """, [(ref, ref.split(":", 1)[0], now_, now_) for ref in refs])
    con.commit()


def clear(con: sqlite3.Connection, source: str, kind: str) -> int:
    """Drop every fact of one (source, kind). Returns how many went.

    For DERIVED facts only. Source facts — a mail message, a calendar event —
    are immutable history and get upserted. Derived facts are a conclusion the
    collector reached about the source data ("this thread is waiting on the user"),
    and a conclusion that stops being recomputed silently becomes a lie: the row
    keeps asserting yesterday's answer forever because nothing ever revisits it.

    So: upsert what happened, rebuild what it means.
    """
    cur = con.execute("DELETE FROM facts WHERE source = ? AND kind = ?", (source, kind))
    return cur.rowcount


def record_run(con: sqlite3.Connection, **fields) -> int:
    # `fell_back` is deliberately not here. It is NOT NULL DEFAULT 0 and existed
    # for the Codex fallback, which was removed on 9 September 2026; listing it
    # meant inserting NULL into it on every run, which failed the constraint and
    # silently cost every run its row -- silently because _record swallows its
    # own errors on purpose, since losing a spend line is better than losing a
    # finished answer. The column stays for the rows that already have it.
    cols = ("label", "engine", "model", "session_id", "duration_ms", "cost_usd",
            "input_tokens", "output_tokens", "cache_read", "cache_write",
            "context_tokens", "num_turns", "exit_code", "error",
            "startup_ms", "model_ms", "tool_ms", "round_trips")
    vals = [fields.get(c) for c in cols]
    cur = con.execute(
        f"INSERT INTO runs (ts, {', '.join(cols)}) VALUES (?{', ?' * len(cols)}) RETURNING id",
        [now(), *vals],
    )
    row_id = _one(cur)
    con.commit()
    return row_id


def record_phases(con: sqlite3.Connection, run_id: int,
                  phases: list[dict]) -> None:
    """Store a run's phase timeline.

    Best-effort on purpose, like `record_run`: a lost timing row is better than
    a lost answer, so a caller that fails here keeps going.
    """
    if not run_id or not phases:
        return
    con.executemany(
        "INSERT INTO run_phases (run_id, seq, kind, name, ms, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(run_id, i, p["kind"], p.get("name"), int(p["ms"]),
          (p.get("detail") or None))
         for i, p in enumerate(phases)],
    )
    con.commit()


def mark_collector(con: sqlite3.Connection, name: str, ok: bool,
                   cursor: str | None = None, error: str | None = None) -> None:
    t = now()
    if ok:
        con.execute(
            """
            INSERT INTO collector_state (collector, last_run, last_ok, cursor,
                                         consecutive_failures, last_error)
            VALUES (?, ?, ?, ?, 0, NULL)
            ON CONFLICT (collector) DO UPDATE SET
                last_run = excluded.last_run, last_ok = excluded.last_ok,
                cursor = COALESCE(excluded.cursor, collector_state.cursor),
                consecutive_failures = 0, last_error = NULL
            """,
            (name, t, t, cursor),
        )
    else:
        con.execute(
            """
            INSERT INTO collector_state (collector, last_run, consecutive_failures, last_error)
            VALUES (?, ?, 1, ?)
            ON CONFLICT (collector) DO UPDATE SET
                last_run = excluded.last_run,
                consecutive_failures = collector_state.consecutive_failures + 1,
                last_error = excluded.last_error
            """,
            (name, t, error),
        )
    con.commit()


def query(sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
    with session() as con:
        return con.execute(sql, tuple(params)).fetchall()
