#!/usr/bin/env python
"""Texts — from the Messages database on this Mac.

Most people's real conversations happen here, and almost none of it is worth
storing. What a personal agent actually needs from a message archive is small:

  thread   who you talk to, when you last spoke, and **whether the last word
           was theirs**. That flag is the reason this exists. Everything else
           in a digest is something you could have looked up; "three people are
           waiting on a reply and one of them asked you a question on Friday"
           is not.

  message  the last few weeks of text, so a conversation can be quoted back or
           reasoned about without the agent going and reading the database
           itself mid-conversation.

Nothing older than the configured window is ingested and nothing is ever
written back. Sending is a separate script behind a separate switch that
defaults to off -- see `bin/imessage-send`.

**Awaiting a reply is a judgement, and a naive version of it is useless.**
"The last message wasn't from me" flags every delivery notification, every
two-factor code and every group chat that carried on without you. So a
conversation only counts as waiting when the last word was theirs, recently,
in a conversation small enough that the words were plausibly meant for you,
and the message either asked something, used your name, or was long enough
that silence would read as dropping it. The alternative -- a list of forty
"unanswered" conversations, most of them robots -- is one you stop reading, and
then the one that mattered is invisible too.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import re
import shutil
import sys

# An extension lives outside the program, so it locates it rather than assuming
# a relative path to it. `herald collect` already puts both on PYTHONPATH; this
# is what makes running the file directly work too.
_EXT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_EXT / "lib"))
_root = os.environ.get("HERALD_ROOT")
if not _root and (_herald := shutil.which("herald")):
    _root = str(pathlib.Path(_herald).resolve().parents[1])
if _root:
    sys.path.insert(0, str(pathlib.Path(_root) / "lib"))

import chatdb  # noqa: E402 -- extensions/imessage/lib/chatdb.py
from herald import collector, config, db  # noqa: E402

NAME = "imessage"
REQUIRES = ("imessage",)

# How often this is worth running: messages arrive continuously and the read is
# local and cheap, so this is about how fresh "waiting on a reply" needs to be.
CADENCE_MINUTES = 30

DEFAULT_DAYS = 30
REPLY_WINDOW_DAYS = 14
#: Above this many participants a conversation is a room, not a question.
GROUP_REPLY_LIMIT = 4
BODY_CAP = 2000

# Short-code senders, no-reply addresses and the shapes automated messages
# arrive as. A five-digit "number" is a short code; nothing personal comes from
# one.
_AUTOMATED = re.compile(r"^(\+?1?\d{3,6}|.*no-?reply.*|.*@.*\.(?:notify|alerts).*)$", re.I)
_ASKS = re.compile(r"\?|\b(can you|could you|would you|are you|will you|when|"
                   r"what time|let me know|lmk|you free|you around)\b", re.I)
_CODEISH = re.compile(r"\b(code|verification|otp|passcode)\b.*\b\d{4,8}\b", re.I)


def _iso(when: dt.datetime | None) -> str | None:
    if not when:
        return None
    return when.astimezone(config.tz()).isoformat(timespec="seconds")


def _addressed_by_name(text: str, names: list[str]) -> bool:
    """Whether the message opens by naming the user.

    The names come from config (`user.name` and anything in
    `extensions.imessage.also_called`), because a name is evidence and "hey"
    is not, and no amount of cleverness recovers the difference from the text
    alone.
    """
    head = (text or "")[:60].lower()
    return any(n and n.lower() in head for n in names)


def _needs_reply(thread: dict, names: list[str], now: dt.datetime) -> tuple[bool, dict]:
    text = thread.get("preview") or ""
    handles = thread.get("handle_list") or []
    automated = bool(handles) and all(_AUTOMATED.match(h) for h in handles)
    days = (now - thread["last_at"]).total_seconds() / 86400 if thread.get("last_at") else 999
    asks = bool(_ASKS.search(text))
    named = _addressed_by_name(text, names)
    substantial = len(text.strip()) >= 60
    why = {
        "automated": automated,
        "days_since": round(days, 1),
        "asks_something": asks,
        "addressed_by_name": named,
        "participants": len(handles),
    }
    ok = (not thread.get("last_from_me")
          and not automated
          and not _CODEISH.search(text)
          and days <= REPLY_WINDOW_DAYS
          and len(handles) <= GROUP_REPLY_LIMIT
          and (asks or named or substantial))
    return ok, why


def collect(con) -> dict:
    days = int(config.get("extensions.imessage.days", DEFAULT_DAYS) or DEFAULT_DAYS)
    names = [config.get("user.name", "").split(" ")[0]]
    names += list(config.get("extensions.imessage.also_called", []) or [])

    chat = chatdb.connect()
    try:
        threads = chatdb.threads(chat)
        recent = chatdb.messages(chat, days=days)
    finally:
        chat.close()

    now = dt.datetime.now(dt.timezone.utc)

    # Messages are history: upsert them, keyed on the message guid, so a
    # re-read of the same window changes nothing.
    stored = 0
    for m in recent:
        if not (m.get("text") or "").strip():
            continue
        db.put_fact(
            con, NAME, "message", external_id=m["guid"] or str(m["id"]),
            ts=_iso(m["at"]),
            title=m.get("handle") or "me",
            body=m["text"][:BODY_CAP],
            data={"direction": "outgoing" if m["is_from_me"] else "incoming",
                  "service": m.get("service"), "chat_id": m.get("chat_id")},
        )
        stored += 1

    # Threads are a conclusion, not history: "waiting on a reply" stops being
    # true the moment you answer, so the whole set is rebuilt each pass rather
    # than upserted. A conclusion that stops being recomputed keeps asserting
    # yesterday's answer forever.
    db.clear(con, NAME, "thread")
    awaiting = 0
    for t in threads:
        if not t.get("last_at"):
            continue
        needs, why = _needs_reply(t, names, now)
        awaiting += int(needs)
        db.put_fact(
            con, NAME, "thread", external_id=t["guid"] or str(t["chat_id"]),
            ts=_iso(t["last_at"]), title=t.get("name"),
            body=(t.get("preview") or "")[:BODY_CAP],
            data={"handles": t.get("handle_list"), "group": t.get("is_group"),
                  "service": t.get("service"),
                  "last_direction": "outgoing" if t.get("last_from_me") else "incoming",
                  "messages_total": t.get("message_count"),
                  "awaiting_reply": needs, **why},
        )

    # Freshness, as a fact rather than an assumption: this reads a live
    # database on the machine Herald is running on, so if the newest message is
    # days old that is a real signal about the person, not a broken sync.
    newest = max((t["last_at"] for t in threads if t.get("last_at")), default=None)
    db.clear(con, NAME, "health")
    db.put_fact(con, NAME, "health", external_id="chatdb", ts=_iso(newest),
                title="fresh", data={"newest_message": _iso(newest),
                                     "threads": len(threads),
                                     "window_days": days})

    return {"threads": len(threads), "recent messages": stored,
            "awaiting reply": awaiting}


if __name__ == "__main__":
    collector.main(NAME, collect)
