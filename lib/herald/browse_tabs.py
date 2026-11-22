"""Who owns which browser tab, so parallel agents share one browser safely.

Before this (21 Nov 2026) a tab belonged to nobody. Every agent saw every tab,
`tabs` indexes shifted under each other, and a session that ended left its
tabs open for good: the three that froze the shared browser on 20 Nov had
been abandoned for a week. One lock covered a whole browser, so a collector
reading pages for four minutes made every other command fail as busy.

Now each tab has an owner:

- a **session**: the name an agent's commands share. It defaults to the
  Telegram topic or engine conversation the command runs in, so two agents
  get separate tab lists without doing anything.
- or a **process** (`pid`): a library caller such as a collector or the
  application filler, whose tabs are reaped as soon as it is gone.

A tab is closed when its owner has not touched it for `browser.tab_idle_hours`
(nobody's tabs sooner), unless something says it must stay: `keep`, a person
driving it through a sign-in link, or an unfinished application bound to it.
Mutual exclusion is per tab, not per browser, through one lock file each.

The registry is its own small SQLite file under `$HERALD_HOME/run`, not the
ledger: it is runtime state, written on every command, and losing it only
means tabs become nobody's and are closed after the shorter grace period.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import sqlite3
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tabs (
    workspace TEXT NOT NULL, tab TEXT NOT NULL, target TEXT,
    session TEXT NOT NULL DEFAULT '', pid INTEGER, url TEXT,
    created REAL NOT NULL, used REAL NOT NULL,
    keep_until REAL, human_until REAL, human_why TEXT,
    strikes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workspace, tab)
);
"""


def session_name(explicit: str | None = None) -> str:
    """The session a command belongs to.

    Explicit, then HERALD_BROWSE_SESSION, then the Telegram topic the turn
    answers (so a topic keeps its tabs from one message to the next), then the
    engine's own conversation id, then one shared name for a bare terminal.
    """
    value = explicit or os.environ.get("HERALD_BROWSE_SESSION")
    if not value:
        topic = os.environ.get("HERALD_TELEGRAM_TOPIC")
        engine = (os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID")
                  or os.environ.get("CODEX_SESSION_ID") or os.environ.get("KIRO_SESSION_ID"))
        if topic:
            value = "topic-" + topic.rsplit(":", 1)[-1]
        elif engine:
            value = "agent-" + engine[:8]
        else:
            value = "terminal"
    value = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")[:40]
    return value or "terminal"


def _path():
    folder = config.HOME / "run"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder / "browser-tabs.db"


@contextlib.contextmanager
def _db():
    con = sqlite3.connect(_path(), timeout=5, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        yield con
    finally:
        con.close()


def short(target: str) -> str:
    return target[:6].lower()


def claim(workspace: str, target: str, session: str, *, pid: int | None = None,
          url: str | None = None) -> str:
    """Record that `session` (or process `pid`) owns a tab. Returns its short id."""
    now = time.time()
    with _db() as c:
        c.execute("""INSERT INTO tabs(workspace,tab,target,session,pid,url,created,used)
                     VALUES (?,?,?,?,?,?,?,?)
                     ON CONFLICT(workspace,tab) DO UPDATE SET
                       session=excluded.session, pid=excluded.pid, used=excluded.used,
                       url=COALESCE(excluded.url, url), strikes=0""",
                  (workspace, short(target), target, session, pid, url, now, now))
    return short(target)


def touch(workspace: str, tab: str, url: str | None = None) -> None:
    with _db() as c:
        c.execute("UPDATE tabs SET used=?, url=COALESCE(?,url), strikes=0 WHERE workspace=? AND tab=?",
                  (time.time(), url, workspace, tab))


def get(tab: str, workspace: str | None = None) -> dict | None:
    with _db() as c:
        if workspace:
            r = c.execute("SELECT * FROM tabs WHERE workspace=? AND tab=?", (workspace, tab)).fetchone()
        else:
            r = c.execute("SELECT * FROM tabs WHERE tab=? ORDER BY used DESC", (tab,)).fetchone()
        return dict(r) if r else None


def rows(*, session: str | None = None, workspace: str | None = None) -> list[dict]:
    q, args = "SELECT * FROM tabs WHERE 1=1", []
    if session is not None:
        q += " AND session=?"
        args.append(session)
    if workspace is not None:
        q += " AND workspace=?"
        args.append(workspace)
    with _db() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY created", args)]


def forget(workspace: str, tab: str) -> None:
    with _db() as c:
        c.execute("DELETE FROM tabs WHERE workspace=? AND tab=?", (workspace, tab))
    with contextlib.suppress(OSError):
        os.unlink(_lock_path(workspace, tab))


def keep(workspace: str, tab: str, hours: float) -> None:
    with _db() as c:
        c.execute("UPDATE tabs SET keep_until=? WHERE workspace=? AND tab=?",
                  (time.time() + hours * 3600 if hours > 0 else None, workspace, tab))


def reserve(workspace: str, tab: str, until: float, why: str) -> None:
    """Hand one tab to the person. Agents are refused on it until `release`."""
    with _db() as c:
        c.execute("""INSERT INTO tabs(workspace,tab,session,created,used,human_until,human_why)
                     VALUES (?,?,'',?,?,?,?)
                     ON CONFLICT(workspace,tab) DO UPDATE SET human_until=excluded.human_until,
                       human_why=excluded.human_why""",
                  (workspace, tab, time.time(), time.time(), until, why[:200]))


def release(workspace: str, tab: str) -> None:
    with _db() as c:
        c.execute("UPDATE tabs SET human_until=NULL, human_why=NULL, used=? WHERE workspace=? AND tab=?",
                  (time.time(), workspace, tab))


def with_person(row: dict | None) -> bool:
    return bool(row and row.get("human_until") and row["human_until"] > time.time())


def sync(workspace: str, live: dict[str, str]) -> None:
    """Make the registry match the browser. `live` maps full target id to URL.

    Tabs nobody claimed (a popup, one the person opened, anything from before
    this registry) are recorded as nobody's from the moment they are first
    seen; rows whose tab is gone are dropped.
    """
    now = time.time()
    with _db() as c:
        known = {r["tab"] for r in c.execute("SELECT tab FROM tabs WHERE workspace=?", (workspace,))}
        seen = set()
        for target, url in live.items():
            tab = short(target)
            seen.add(tab)
            if tab in known:
                c.execute("UPDATE tabs SET url=?, target=? WHERE workspace=? AND tab=?",
                          (url, target, workspace, tab))
            else:
                c.execute("INSERT INTO tabs(workspace,tab,target,session,url,created,used) VALUES (?,?,?,'',?,?,?)",
                          (workspace, tab, target, url, now, now))
        for tab in known - seen:
            c.execute("DELETE FROM tabs WHERE workspace=? AND tab=?", (workspace, tab))
            with contextlib.suppress(OSError):
                os.unlink(_lock_path(workspace, tab))


def strike(workspace: str, tab: str) -> int:
    """Count one more look at which this tab did not answer. Returns the total."""
    with _db() as c:
        c.execute("UPDATE tabs SET strikes=strikes+1 WHERE workspace=? AND tab=?", (workspace, tab))
        r = c.execute("SELECT strikes FROM tabs WHERE workspace=? AND tab=?", (workspace, tab)).fetchone()
        return r["strikes"] if r else 0


def pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def idle_hours(owned: bool) -> float:
    key, default = ("tab_idle_hours", 6) if owned else ("unowned_idle_hours", 2)
    return float(config.get("browser." + key, default))


def expired(workspace: str, protected=()) -> list[dict]:
    """Tabs whose owner is done with them. `protected` holds short ids that
    must stay whatever their age (an unfinished application, an open view)."""
    now = time.time()
    out = []
    for r in rows(workspace=workspace):
        if r["tab"] in protected or with_person(r) or (r["keep_until"] or 0) > now:
            continue
        if r["pid"]:
            if pid_alive(r["pid"]):
                continue
            r["why"] = "the process that opened it is gone"
            # A tab a finished process meant to leave open is protected by
            # whatever it was left open for (see `protected`), not by the pid.
            if now - r["used"] > 60:
                out.append(r)
            continue
        limit = idle_hours(bool(r["session"])) * 3600
        if now - r["used"] > limit:
            r["why"] = (f"session {r['session']} left it idle for {(now - r['used']) / 3600:.1f} hours"
                        if r["session"] else "nobody claimed it")
            out.append(r)
    return out


# --- one lock per tab ---------------------------------------------------------

def _lock_path(workspace: str, tab: str) -> str:
    folder = config.HOME / "run" / "browser-locks"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return str(folder / f"tab-{workspace}-{tab}.lock")


class TabBusy(RuntimeError):
    pass


def try_lock(workspace: str, tab: str):
    """Take a tab's lock without waiting. Returns the fd, or None if it is held."""
    fd = os.open(_lock_path(workspace, tab), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    from .browser_workspaces import note_holder
    note_holder(fd)
    return fd


def lock_holder(workspace: str, tab: str) -> str:
    from .browser_workspaces import holder
    try:
        fd = os.open(_lock_path(workspace, tab), os.O_RDONLY)
    except OSError:
        return "holder unknown"
    try:
        return holder(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def lock(workspace: str, tab: str, timeout: float = 30):
    """Hold one tab for one writer, across processes."""
    deadline = time.monotonic() + timeout
    while True:
        fd = try_lock(workspace, tab)
        if fd is not None:
            break
        if time.monotonic() >= deadline:
            raise TabBusy(f"tab {tab} is busy: {lock_holder(workspace, tab)}. Retry when that finishes.")
        time.sleep(0.1)
    try:
        yield
    finally:
        os.close(fd)
