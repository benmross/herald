#!/usr/bin/env python
"""Gmail — recent mail, and which threads are actually waiting on the user.

Metadata and snippets only. Full bodies are large, mostly boilerplate, and one
API call away when the agent wants one — storing them would trade real disk and
context for very little the snippet does not already carry.

Other addresses that forward into this mailbox arrive here too, so one inbox
usually sees everything.

The derived value is thread state. "Last message wasn't from me" is far too
generous a definition of waiting: on a student inbox it flags every listserv
digest and every no-reply notification. Gmail has already done the hard part of
this classification, so the filter leans on its categories first and on the
bulk-mail headers second, and only then calls something a thread the user owes a
reply to.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import collector, config, db, google  # noqa: E402

NAME = "gmail"
REQUIRES = ('google',)
# How far back thread state is rebuilt. This is not a re-read: the history API
# supplies the deltas and the ledger already holds the messages. It only decides
# how long a thread stays eligible to be "waiting on the user" -- and an email the user has
# not answered in five weeks is more of an open loop than one from Tuesday, not
# less. Ninety days covers a semester.
THREAD_DAYS = 90

# How often this is worth running:
# incremental through the history API; a quiet pass fetches nothing
CADENCE_MINUTES = 30
WINDOW = "newer_than:21d"
# messages.list defaults to includeSpamTrash=False, so the backfill silently
# skipped every message the user had already trashed or Gmail had filed as spam --
# 158 of them in the first three weeks. Archived mail was always covered (it is
# still in All Mail); trash and spam were not. They are worth having: a receipt
# they binned is still what they bought, and the phishing sent to an address
    # they never read is
# worth being able to see. Nothing here can become a thread "waiting on the user" --
# that test requires INBOX -- so including them adds knowledge, not noise.
# Gmail keeps history for a while, not forever. When the stored id is too old
# the API says so and we fall back to reading the window again.
HISTORY_TYPES = ("messageAdded", "labelAdded", "labelRemoved")
BATCH = 50
PAUSE = 0.3   # seconds between batches; Gmail 429s a tight loop
HEADERS = ["From", "To", "Cc", "Subject", "Date", "List-Id", "List-Unsubscribe",
           "Precedence", "Reply-To", "Auto-Submitted"]

# Gmail's own tabs. Anything filed under one of these is broadcast, not
# correspondence.
BULK_CATEGORIES = {"CATEGORY_PROMOTIONS", "CATEGORY_UPDATES",
                   "CATEGORY_FORUMS", "CATEGORY_SOCIAL"}

_NOREPLY = re.compile(
    r"(no[-_.]?reply|donotreply|do[-_.]?not[-_.]?reply|mailer-daemon|postmaster"
    r"|listserv|bounce|notification|@notifications?\.|automail|auto-confirm)",
    re.I)


def _ts(ms: int) -> str | None:
    if not ms:
        return None
    return dt.datetime.fromtimestamp(ms / 1000, config.tz()).isoformat(timespec="seconds")


def _header(msg: dict, name: str) -> str | None:
    want = name.lower()
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == want:
            return h.get("value")
    return None


def _is_me(addr: str | None, me: str) -> bool:
    return bool(addr) and me.lower() in addr.lower()


def _is_bulk(msg: dict, labels: set[str]) -> bool:
    """Broadcast rather than correspondence.

    Any one of these is sufficient: Gmail filed it under a non-personal tab, it
    carries mailing-list or auto-submitted headers, or the sender advertises
    that replying is pointless.
    """
    if labels & BULK_CATEGORIES:
        return True
    if _header(msg, "List-Id") or _header(msg, "List-Unsubscribe"):
        return True
    if (_header(msg, "Precedence") or "").lower() in ("bulk", "list", "junk"):
        return True
    if _header(msg, "Auto-Submitted") not in (None, "no"):
        return True
    return bool(_NOREPLY.search(_header(msg, "From") or ""))


def _changed_since(svc, start_id: str) -> tuple[list[str], str | None]:
    """Message ids touched since start_id, or (None) if the id is too old.

    This is the difference between re-reading three weeks of mail every half
    hour and reading the four messages that actually arrived.
    """
    ids, page = set(), None
    latest = start_id
    while True:
        try:
            resp = svc.users().history().list(
                userId="me", startHistoryId=start_id, pageToken=page,
                historyTypes=list(HISTORY_TYPES), maxResults=500).execute()
        except Exception as e:                            # noqa: BLE001
            # 404 means the stored id has aged out of Gmail's history window.
            if "404" in str(e) or "startHistoryId" in str(e):
                return [], None
            raise
        for h in resp.get("history", []):
            for key in ("messagesAdded", "labelsAdded", "labelsRemoved"):
                for entry in h.get(key, []) or []:
                    ids.add(entry["message"]["id"])
        latest = resp.get("historyId", latest)
        page = resp.get("nextPageToken")
        if not page:
            break
    return sorted(ids), latest


def collect(con) -> dict:
    svc = google.service("gmail", "v1")

    # Messages carry label *ids* ("Label_1735656434075..."); the names live
    # here. One call, rebuilt each run, so a query can say "labelled Maryland"
    # instead of memorising an id. The campus-events sync in the scout cycle
    # depends on it.
    db.clear(con, NAME, "label")
    for lb in svc.users().labels().list(userId="me").execute().get("labels", []):
        db.put_fact(con, NAME, "label", external_id=lb["id"], title=lb.get("name"),
                    data={"type": lb.get("type")})
    profile = svc.users().getProfile(userId="me").execute()
    me = profile["emailAddress"]
    head_history = profile.get("historyId")

    since = collector.cursor(con, NAME)
    ids, mode = [], "full"

    if since:
        changed, latest = _changed_since(svc, since)
        if latest is not None:
            ids, mode = changed, "incremental"

    if mode == "full":
        page = None
        while True:
            resp = svc.users().messages().list(
                userId="me", q=WINDOW, maxResults=500, pageToken=page,
                includeSpamTrash=True).execute()
            ids.extend(m["id"] for m in resp.get("messages", []))
            page = resp.get("nextPageToken")
            if not page:
                break

    fetched: dict[str, dict] = {}
    failed: list[str] = []
    last_error = ""

    def _capture(request_id, response, exception):
        nonlocal last_error
        if exception is not None:
            failed.append(request_id)
            last_error = str(exception)[:200]
        elif response:
            fetched[response["id"]] = response

    def _get(mid):
        return svc.users().messages().get(
            userId="me", id=mid, format="metadata", metadataHeaders=HEADERS)

    for i in range(0, len(ids), BATCH):
        batch = svc.new_batch_http_request(callback=_capture)
        for mid in ids[i:i + BATCH]:
            # Use the message id as the request id so a failure is identifiable
            # and therefore retryable.
            batch.add(_get(mid), request_id=mid)
        batch.execute()
        time.sleep(PAUSE)

    # Gmail rate-limits batched per-message reads (HTTP 429) often enough that a
    # first pass is routinely a few short. Retrying those serially costs a second
    # and removes the drift between runs that made counts look unstable.
    if failed:
        retry, failed = failed, []
        for mid in retry:
            try:
                fetched[mid] = _get(mid).execute()
            except Exception as e:                       # noqa: BLE001
                failed.append(mid)
                last_error = str(e)[:200]
            time.sleep(PAUSE)

    bulk_count = 0

    for mid, msg in fetched.items():
        labels = set(msg.get("labelIds", []))
        sender = _header(msg, "From")
        ts_ms = int(msg.get("internalDate", 0))
        bulk = _is_bulk(msg, labels)
        bulk_count += int(bulk)

        db.put_fact(
            con, NAME, "message", external_id=mid, ts=_ts(ts_ms),
            title=_header(msg, "Subject"), body=msg.get("snippet"),
            data={
                "thread_id": msg.get("threadId"),
                "from": sender,
                "to": _header(msg, "To"),
                "cc": _header(msg, "Cc"),
                "reply_to": _header(msg, "Reply-To"),
                "list_id": _header(msg, "List-Id"),
                "labels": sorted(labels),
                "from_me": _is_me(sender, me),
                "bulk": bulk,
                "unread": "UNREAD" in labels,
                "in_inbox": "INBOX" in labels,
                "in_trash": "TRASH" in labels,
                "in_spam": "SPAM" in labels,
            },
        )

    # Thread state is derived, so rebuild it rather than upserting: a thread that
    # drops out of the window must stop claiming it is waiting on a reply.
    #
    # Rebuild from the LEDGER, not from what this run fetched. An incremental run
    # may have touched four messages; rebuilding thread state from those four
    # would delete every other thread the user has.
    threads: dict[str, list[dict]] = {}
    for r in con.execute("""
        SELECT ts, title, data FROM facts
        WHERE source = ? AND kind = 'message'
          AND ts >= date('now', '-' || ? || ' days')
    """, (NAME, THREAD_DAYS)):
        d = json.loads(r["data"] or "{}")
        threads.setdefault(d.get("thread_id") or "?", []).append({
            "ts_ms": int(dt.datetime.fromisoformat(r["ts"]).timestamp() * 1000)
                     if r["ts"] else 0,
            "from": d.get("from"), "from_me": bool(d.get("from_me")),
            "bulk": bool(d.get("bulk")), "in_inbox": bool(d.get("in_inbox")),
            "subject": r["title"],
        })

    db.clear(con, NAME, "thread")

    awaiting = 0
    for tid, msgs in threads.items():
        msgs.sort(key=lambda m: m["ts_ms"])
        last = msgs[-1]
        # Waiting on the user means: a person wrote to them, it is still in their inbox,
        # and the user has not answered.
        needs_reply = (not last["from_me"]) and last["in_inbox"] and not last["bulk"]
        awaiting += int(needs_reply)
        db.put_fact(
            con, NAME, "thread", external_id=tid, ts=_ts(last["ts_ms"]),
            title=last["subject"],
            data={
                "messages": len(msgs),
                "last_from": last["from"],
                "last_from_me": last["from_me"],
                "bulk": last["bulk"],
                "in_inbox": last["in_inbox"],
                "awaiting_reply": needs_reply,
                "user_has_replied": any(m["from_me"] for m in msgs),
            },
        )

    if failed:
        # Loud, but not fatal: a partial inbox is still worth having, and the
        # next run picks up what this one missed.
        print(f"  {len(failed)} of {len(ids)} message fetches failed after retry;"
              f" last: {last_error}", file=sys.stderr)

    return {"mode": mode, "fetched": len(fetched), "threads": len(threads),
            "bulk": bulk_count, "awaiting reply": awaiting,
            "fetch failures": len(failed),
            "_cursor": head_history}


if __name__ == "__main__":
    collector.main(NAME, collect)
