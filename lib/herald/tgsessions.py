"""Which Telegram message belongs to which session, so a reply can find it.

A topic points at one session at a time, and `/new` moves the pointer. Until
7 October 2026 that was the end of the old conversation: its transcript was
still on disk and the engine could still resume it, but nothing recorded which
session a message in the chat had come from, so there was no way to ask for it
back. These two tables are that record.

`telegram_messages` maps every message of a turn (what the user sent, the
progress block, each reply) to the session the turn ran in. `telegram_sessions`
keeps what the bridge needs to continue a session it is no longer pointing at:
the engine that owns the transcript, the model, the turn count, and the
orientation card, which has to come back byte-for-byte or the resumed session's
prompt cache is invalidated on its first message.
"""

from __future__ import annotations

import sqlite3
import time


def save(con: sqlite3.Connection, session_id: str, topic: str, *,
         engine: str | None, model: str | None, turns: int,
         orientation: str | None) -> None:
    """Record a session as it stands after a turn."""
    con.execute("""
        INSERT INTO telegram_sessions
            (session_id, topic, engine, model, turns, last_ts, orientation)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (session_id) DO UPDATE SET
            topic = excluded.topic, engine = excluded.engine,
            model = excluded.model, turns = excluded.turns,
            last_ts = excluded.last_ts,
            orientation = COALESCE(excluded.orientation, orientation)
    """, (session_id, topic, engine, model, turns, time.time(), orientation))


def record(con: sqlite3.Connection, chat_id: int, message_ids,
           session_id: str) -> None:
    """Attach messages to a session. Ids are unique within a chat, so a
    conflict only ever means the same message recorded twice."""
    con.executemany(
        "INSERT OR REPLACE INTO telegram_messages (chat_id, message_id, session_id) "
        "VALUES (?, ?, ?)",
        [(chat_id, mid, session_id) for mid in message_ids if mid])


def lookup(con: sqlite3.Connection, chat_id: int, message_id: int):
    """The session a message belongs to, or None if it was never recorded
    (anything sent before these tables existed, a digest, a quiz question)."""
    return con.execute("""
        SELECT s.* FROM telegram_messages m
        JOIN telegram_sessions s ON s.session_id = m.session_id
        WHERE m.chat_id = ? AND m.message_id = ?
    """, (chat_id, message_id)).fetchone()
