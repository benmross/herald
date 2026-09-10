"""Reading the Messages database on a Mac.

Everything specific to Apple's schema lives here, so the collector above it can
be about conversations rather than about SQLite. Four things make this database
harder than it looks, and each one is a wrong answer somebody ships:

**1. The clock.** `message.date` is nanoseconds since 2001-01-01 UTC on any
macOS since High Sierra, and seconds since the same epoch before that. The
values differ by nine orders of magnitude, so the fix is to look at the
magnitude rather than at the OS version.

**2. The text is often not in the text column.** Since Ventura, `message.text`
is frequently NULL and the content lives in `attributedBody`, a serialised
NSAttributedString. Parsing that properly means a plist decoder and a NeXTSTEP
typed-stream reader; what actually works, and what this does, is to find the
NSString payload inside it and read its length-prefixed bytes. A message whose
body cannot be recovered is reported as empty rather than dropped, because a
conversation with a gap in it is still evidence about who spoke last.

**3. The database is live.** Messages holds it open in WAL mode. Opening it
read-write risks corrupting somebody's entire message history, so this opens it
with `mode=ro`, and falls back to `immutable=1` when the WAL cannot be read --
which is stale by up to a few minutes rather than wrong.

**4. Reading it at all requires permission macOS does not grant by default.**
Full Disk Access, for whichever process runs Herald. There is no way to ask for
it programmatically, and the failure is a bare `unable to open database file`,
so `check_access()` turns that into a sentence that says what to click.

Read-only, always. Nothing in this module writes to the database or to
Messages; sending is a separate script with a separate switch.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re
import sqlite3

CHAT_DB = pathlib.Path.home() / "Library" / "Messages" / "chat.db"

#: 2001-01-01T00:00:00Z, the epoch Core Data counts from.
APPLE_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)

#: Anything larger than this many "seconds" is really nanoseconds. A genuine
#: seconds-since-2001 timestamp reaches 1e9 around the year 2033; a
#: nanoseconds one passed 1e17 long ago. Nothing lands between.
_NANO_THRESHOLD = 1e11


class AccessError(RuntimeError):
    """The database exists but macOS will not let this process read it."""


def apple_time(value: int | float | None) -> dt.datetime | None:
    if not value:
        return None
    seconds = value / 1e9 if abs(value) > _NANO_THRESHOLD else value
    try:
        return APPLE_EPOCH + dt.timedelta(seconds=seconds)
    except OverflowError:
        return None


# The typed-stream archive wraps the message text in an NSString whose bytes
# are preceded by a length: one byte, or 0x81 followed by a little-endian
# uint16, or 0x82 followed by a uint32. Everything before `NSString` is class
# metadata and everything after the run is attribute data.
_NSSTRING = re.compile(rb"NSString\x01\x94\x84\x01\x2b(.)", re.S)


def attributed_body_text(blob: bytes | None) -> str:
    """Best-effort message text out of an attributedBody blob."""
    if not blob:
        return ""
    match = _NSSTRING.search(blob)
    if not match:
        # Older shapes put the marker without the class-metadata run.
        idx = blob.find(b"NSString")
        if idx == -1:
            return ""
        start = blob.find(b"+", idx)
        if start == -1:
            return ""
        match_start = start + 1
    else:
        match_start = match.start(1)

    first = blob[match_start]
    if first == 0x81:
        length = int.from_bytes(blob[match_start + 1:match_start + 3], "little")
        start = match_start + 3
    elif first == 0x82:
        length = int.from_bytes(blob[match_start + 1:match_start + 5], "little")
        start = match_start + 5
    else:
        length = first
        start = match_start + 1
    raw = blob[start:start + length]
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace").strip()


def connect(path: pathlib.Path | None = None) -> sqlite3.Connection:
    """A read-only connection, never a writable one.

    Messages keeps this database open in WAL mode and it is the only copy of
    somebody's message history. `mode=ro` first, because it sees the live WAL;
    `immutable=1` as a fallback for the case where the -wal file is not
    readable, at the cost of missing the last few minutes.
    """
    path = path or CHAT_DB
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist -- this extension only works on a Mac "
            f"signed in to Messages.")
    for uri in (f"file:{path}?mode=ro", f"file:{path}?mode=ro&immutable=1"):
        try:
            con = sqlite3.connect(uri, uri=True, timeout=30)
            con.row_factory = sqlite3.Row
            con.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return con
        except sqlite3.OperationalError as exc:
            last = exc
    raise AccessError(
        f"cannot read {path}: {last}. macOS requires Full Disk Access for the "
        f"program that runs Herald: System Settings -> Privacy & Security -> "
        f"Full Disk Access, add your terminal (or whatever launches Herald), "
        f"then restart it.")


def check_access(path: pathlib.Path | None = None) -> str | None:
    """None if readable, otherwise a sentence saying what to do about it."""
    try:
        connect(path).close()
        return None
    except (FileNotFoundError, AccessError) as exc:
        return str(exc)


# `chat` is the conversation, `handle` is the other party's address. A group
# chat has many handles and usually a display name; a one-to-one chat has one
# handle and no display name, so the handle is the name until contacts say
# otherwise.
THREADS_SQL = """
SELECT c.ROWID                AS chat_id,
       c.guid                 AS guid,
       c.display_name         AS display_name,
       c.chat_identifier      AS chat_identifier,
       c.service_name         AS service,
       COUNT(m.ROWID)         AS message_count,
       MAX(m.date)            AS last_date,
       (SELECT m2.is_from_me FROM message m2
          JOIN chat_message_join j2 ON j2.message_id = m2.ROWID
         WHERE j2.chat_id = c.ROWID
         ORDER BY m2.date DESC LIMIT 1)     AS last_from_me,
       (SELECT COALESCE(m3.text, '') FROM message m3
          JOIN chat_message_join j3 ON j3.message_id = m3.ROWID
         WHERE j3.chat_id = c.ROWID
         ORDER BY m3.date DESC LIMIT 1)     AS last_text,
       (SELECT m4.attributedBody FROM message m4
          JOIN chat_message_join j4 ON j4.message_id = m4.ROWID
         WHERE j4.chat_id = c.ROWID
         ORDER BY m4.date DESC LIMIT 1)     AS last_body,
       (SELECT GROUP_CONCAT(h.id, ', ') FROM chat_handle_join chj
          JOIN handle h ON h.ROWID = chj.handle_id
         WHERE chj.chat_id = c.ROWID)       AS handles
  FROM chat c
  JOIN chat_message_join j ON j.chat_id = c.ROWID
  JOIN message m           ON m.ROWID = j.message_id
 GROUP BY c.ROWID
 HAVING last_date IS NOT NULL
 ORDER BY last_date DESC
"""

MESSAGES_SQL = """
SELECT m.ROWID          AS id,
       m.guid           AS guid,
       m.date           AS date,
       m.is_from_me     AS is_from_me,
       m.text           AS text,
       m.attributedBody AS body,
       m.service        AS service,
       m.is_read        AS is_read,
       h.id             AS handle,
       j.chat_id        AS chat_id
  FROM message m
  JOIN chat_message_join j ON j.message_id = m.ROWID
  LEFT JOIN handle h       ON h.ROWID = m.handle_id
 WHERE m.date > ?
 ORDER BY m.date DESC
 LIMIT ?
"""


def _apple_cutoff(days: int) -> int:
    when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    return int((when - APPLE_EPOCH).total_seconds() * 1e9)


def threads(con: sqlite3.Connection) -> list[dict]:
    out = []
    for row in con.execute(THREADS_SQL):
        row = dict(row)
        row["last_at"] = apple_time(row.pop("last_date"))
        row["preview"] = (row.pop("last_text") or "").strip() \
            or attributed_body_text(row.pop("last_body", None))
        row.pop("last_body", None)
        row["handle_list"] = [h.strip() for h in (row.get("handles") or "").split(",")
                              if h.strip()]
        row["is_group"] = len(row["handle_list"]) > 1
        row["name"] = (row.get("display_name") or "").strip() \
            or (row["handle_list"][0] if row["handle_list"] else row.get("chat_identifier"))
        out.append(row)
    return out


def messages(con: sqlite3.Connection, *, days: int = 30,
             limit: int = 20000) -> list[dict]:
    out = []
    for row in con.execute(MESSAGES_SQL, (_apple_cutoff(days), limit)):
        row = dict(row)
        row["at"] = apple_time(row.pop("date"))
        row["text"] = (row.get("text") or "").strip() \
            or attributed_body_text(row.pop("body", None))
        row.pop("body", None)
        out.append(row)
    return out
