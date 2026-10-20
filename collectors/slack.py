#!/usr/bin/env python
"""Slack: every message in the conversations the user belongs to, as them.

Uses a user token (xoxp-) from a Slack app the user created in their own
workspace, so it sees what they see: public and private channels, group DMs and
DMs. A bot token cannot see DMs. Tokens live in secrets.json under
`slack.tokens`, one per workspace, keyed by a short name the user chooses.

Incremental per conversation: the cursor is the newest message ts seen in each,
so a quiet run costs one `users.conversations` listing plus one history call per
conversation. The first run reaches back `slack.backfill_days` (default 30).
Replies are fetched for each thread parent the history call returns, because
history carries only the parent. A reply added to a thread whose parent is
older than the cursor is therefore not seen; that gap is accepted.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import collector, config, db  # noqa: E402

NAME = "slack"
REQUIRES = ("slack",)
CADENCE_MINUTES = 15

API = "https://slack.com/api/"
CONVERSATION_TYPES = "public_channel,private_channel,mpim,im"
MAX_RETRIES = 4


def _call(token: str, method: str, **params) -> dict:
    """One Slack Web API call, waiting out rate limits."""
    body = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(
            API + method, data=body.encode(),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as err:
            if err.code == 429:
                time.sleep(int(err.headers.get("Retry-After", "5")) + 1)
                continue
            raise
        if not data.get("ok"):
            if data.get("error") == "ratelimited":
                time.sleep(5)
                continue
            raise RuntimeError(f"slack {method}: {data.get('error')}")
        return data
    raise RuntimeError(f"slack {method}: rate limited {MAX_RETRIES} times")


def _pages(token: str, method: str, key: str, **params):
    cursor = None
    while True:
        data = _call(token, method, cursor=cursor, limit=200, **params)
        yield from data.get(key, [])
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return


def _iso(ts: str) -> str:
    return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).isoformat()


def _label(conv: dict, users: dict) -> str:
    if conv.get("is_im"):
        return "DM with " + users.get(conv.get("user"), conv.get("user", "?"))
    if conv.get("is_mpim"):
        return "group DM " + conv.get("name", conv["id"])
    return "#" + conv.get("name", conv["id"])


def _store(con, team: str, conv: dict, label: str, users: dict, msg: dict) -> None:
    if msg.get("subtype") in ("channel_join", "channel_leave"):
        return
    ts = msg["ts"]
    author = users.get(msg.get("user"), msg.get("username") or msg.get("user") or "?")
    thread = msg.get("thread_ts")
    db.put_fact(
        con, NAME, "message", external_id=f"{team}:{conv['id']}:{ts}",
        ts=_iso(ts), title=f"{author} in {label}", body=msg.get("text", ""),
        data={"team": team, "channel": conv["id"], "channel_name": label,
              "user": msg.get("user"), "author": author, "slack_ts": ts,
              "thread_ts": thread if thread and thread != ts else None,
              "reply_count": msg.get("reply_count"),
              "files": [f.get("name") for f in msg.get("files", [])] or None,
              "dm": bool(conv.get("is_im") or conv.get("is_mpim"))})


def _sync_workspace(con, team: str, token: str, seen: dict, backfill: float) -> int:
    users = {u["id"]: u.get("real_name") or u.get("name") or u["id"]
             for u in _pages(token, "users.list", "members")}
    stored = 0
    for conv in _pages(token, "users.conversations", "channels",
                       types=CONVERSATION_TYPES, exclude_archived="true"):
        key = f"{team}:{conv['id']}"
        oldest = seen.get(key) or str(backfill)
        label = _label(conv, users)
        newest = oldest
        threads = []
        for msg in _pages(token, "conversations.history", "messages",
                          channel=conv["id"], oldest=oldest):
            _store(con, team, conv, label, users, msg)
            stored += 1
            newest = max(newest, msg["ts"], key=float)
            if msg.get("reply_count"):
                threads.append(msg["ts"])
        # Replies to threads that started before the cursor never show up in
        # history, so threads are found from the parents history did return.
        for parent in threads:
            for reply in _pages(token, "conversations.replies", "messages",
                                channel=conv["id"], ts=parent, oldest=oldest):
                if reply["ts"] != parent:
                    _store(con, team, conv, label, users, reply)
                    stored += 1
                    newest = max(newest, reply["ts"], key=float)
        seen[key] = newest
    return stored


def collect(con) -> dict:
    tokens = config.secret("slack.tokens") or {}
    backfill = time.time() - 86400 * int(config.get("slack.backfill_days", 30))
    prev = collector.cursor(con, NAME)
    seen = json.loads(prev) if prev else {}
    counts = {}
    for team, token in tokens.items():
        counts[f"{team} messages"] = _sync_workspace(con, team, token, seen, backfill)
    counts["_cursor"] = json.dumps(seen)
    return counts


if __name__ == "__main__":
    collector.main(NAME, collect)
