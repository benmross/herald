"""Herald's one door out to Google.

Reading Google is spread across the collectors. Writing is not: every calendar
event, task, label or draft Herald creates goes through this module, and every
call here records a row in the `actions` table before it returns. That is what
makes CLAUDE.md's amber tier real -- "reversible actions that only the user sees,
in service of a standing instruction, never silent" -- because the dawn
snapshot reads that table and the digest reports what accumulated.

Two things are deliberately absent. There is no function that sends mail or
any other message: sending is red, it needs the user to have asked in the
conversation, and a conversation already has the google-workspace skill for
it. And there is no delete for anything a person could miss: a calendar event
Herald manages is cancelled (retitled and greyed) rather than removed, because
Calendar has no trash and CLAUDE.md says trash, never delete.

The two deletes here are both about things no person could miss, and both
refuse rather than guess. `calendar_delete_own` takes back an event Herald
itself created, proven by a `calendar.create` row in the actions table; it is
undo. `calendar_delete_synced` removes a scraper-written, unedited event in a
category the user has ruled off one of Herald's own firehose calendars; it exists
because greying a whole category as CANCELLED would be false.

`tools/check.py` enforces the "only door" part: no other file may call a
mutating method on a Google service.

Calendar identity, carried over from the sync this replaced so the events it
already wrote are recognised natively:

  extendedProperties.private.syncKey   stable source id, e.g. "feed:12487837"
  extendedProperties.private.syncHash  hash of the fields WE last wrote

If a live event's fields still hash to syncHash, nobody has touched it since
the last write and it may be updated freely. If they differ, the user edited it by
hand: their summary, start, end and description win, and only location and the
source link are refreshed.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import re
from email.message import EmailMessage

from . import config, db, google

TZ = "America/New_York"
CANCEL_PREFIX = "CANCELLED - "

# Fields the user's manual edits are allowed to own, and the ones refreshed anyway.
PROTECTED = ("summary", "start", "end", "description")
ALWAYS_REFRESH = ("location", "source")


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------

def calendar_service():
    return google.service("calendar", "v3")


def calendar_id(name: str) -> str | None:
    """The id of a calendar the user can see, by its display name.

    Read from the gcal collector's facts first (free), then from the API.
    """
    with db.session() as con:
        row = con.execute(
            "SELECT external_id FROM facts WHERE source='gcal' AND kind='calendar'"
            " AND title = ?", (name,)).fetchone()
    if row:
        return row[0]
    page = None
    svc = calendar_service()
    while True:
        resp = svc.calendarList().list(pageToken=page, maxResults=250).execute()
        for cal in resp.get("items", []):
            if cal.get("summary") == name:
                return cal["id"]
        page = resp.get("nextPageToken")
        if not page:
            return None


def field_hash(body: dict) -> str:
    parts = [
        body.get("summary", ""),
        (body.get("start") or {}).get("dateTime", "") or (body.get("start") or {}).get("date", ""),
        (body.get("end") or {}).get("dateTime", "") or (body.get("end") or {}).get("date", ""),
        body.get("location", ""),
        body.get("description", ""),
        body.get("colorId", ""),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]


def summarize(ev: dict) -> dict:
    """Reduce a Google event to what a sync needs to reason about."""
    props = (ev.get("extendedProperties") or {}).get("private") or {}
    body = {
        "summary": ev.get("summary", "") or "",
        "start": ev.get("start", {}) or {},
        "end": ev.get("end", {}) or {},
        "location": ev.get("location", "") or "",
        "description": ev.get("description", "") or "",
        "colorId": ev.get("colorId", "") or "",
    }
    stored = props.get("syncHash", "")
    return {
        "id": ev["id"],
        "syncKey": props.get("syncKey", ""),
        "summary": body["summary"],
        "start": body["start"].get("dateTime") or body["start"].get("date"),
        "end": body["end"].get("dateTime") or body["end"].get("date"),
        "location": body["location"],
        "description": body["description"],
        "colorId": body["colorId"],
        "managed": bool(stored),
        "user_modified": bool(stored) and field_hash(body) != stored,
        "cancelled_since": props.get("cancelledSince", ""),
        "status": ev.get("status"),
    }


def calendar_window(cal_id: str, lo: dt.datetime, hi: dt.datetime) -> list[dict]:
    """Every event on one calendar in [lo, hi), summarised. Read only."""
    svc = calendar_service()
    items, tok = [], None
    while True:
        r = svc.events().list(calendarId=cal_id, timeMin=lo.isoformat(),
                              timeMax=hi.isoformat(), singleEvents=True,
                              orderBy="startTime", showDeleted=False,
                              maxResults=250, pageToken=tok).execute()
        items += r.get("items", [])
        tok = r.get("nextPageToken")
        if not tok:
            break
    return [summarize(e) for e in items]


def build_event(*, summary: str, start: str, end: str | None, all_day: bool,
                location: str = "", description: str = "", color_id: str = "",
                sync_key: str, source_url: str | None = None,
                source_title: str | None = None,
                reminder_minutes: int | None = None) -> dict:
    """A Calendar event body carrying Herald's identity properties."""
    if all_day:
        # Google treats an all-day `end.date` as EXCLUSIVE, but sources carry
        # an inclusive last day ("runs through Oct 3"). Without this a
        # multi-day exhibition renders a day short.
        sd = dt.date.fromisoformat(start[:10])
        ed = dt.date.fromisoformat((end or start)[:10])
        end_is_midnight = (end or "")[11:16] in ("", "00:00")
        if ed <= sd or not end_is_midnight:
            ed = ed + dt.timedelta(days=1)
        s = {"date": sd.isoformat()}
        e = {"date": ed.isoformat()}
    else:
        if not end:
            end = (dt.datetime.fromisoformat(start) + dt.timedelta(hours=1)).isoformat()
        s = {"dateTime": start, "timeZone": TZ}
        e = {"dateTime": end, "timeZone": TZ}
    body = {
        "summary": summary,
        "location": location or "",
        "description": description or "",
        "start": s,
        "end": e,
        "transparency": "transparent",
    }
    if color_id:
        body["colorId"] = color_id
    if source_url:
        body["source"] = {"title": (source_title or "Source")[:60], "url": source_url}
    if reminder_minutes:
        body["reminders"] = {"useDefault": False,
                             "overrides": [{"method": "popup", "minutes": reminder_minutes}]}
    else:
        body["reminders"] = {"useDefault": False, "overrides": []}
    body["extendedProperties"] = {"private": {
        "syncKey": sync_key,
        "syncHash": field_hash(body),
    }}
    return body


def _projected(existing: dict, patch: dict) -> dict:
    """What the live event will look like after a partial patch."""
    def block(v):
        return {"dateTime": v} if "T" in (v or "") else {"date": v}
    return {
        "summary": patch.get("summary", existing["summary"]),
        "start": block(existing["start"]),
        "end": block(existing["end"]),
        "location": patch.get("location", existing["location"]),
        "description": patch.get("description", existing["description"]),
        "colorId": patch.get("colorId", existing["colorId"]),
    }


def merge_for_update(existing: dict, desired: dict) -> dict:
    """The merge policy for a hand-edited event: refresh only what is ours."""
    patch = {k: desired[k] for k in ALWAYS_REFRESH if k in desired}
    patch["extendedProperties"] = {"private": dict(desired["extendedProperties"]["private"])}
    patch["extendedProperties"]["private"]["syncHash"] = field_hash(_projected(existing, patch))
    return patch


def _log(con, actor: str, tier: str, kind: str, target: str, summary: str,
         ref: str | None) -> None:
    db.record_action(con, actor=actor, tier=tier, kind=kind, target=target,
                     summary=summary[:200], ref=ref)
    # Commit now, not when the caller's run ends. The Google write has
    # already happened; if the process is killed before the collector's
    # final commit, the audit row must not roll back with it. On 9 Sep 2026
    # a killed sync left 104 real events on the calendar with no record.
    con.commit()


def calendar_insert(con, *, actor: str, cal_id: str, cal_name: str, body: dict,
                    tier: str = "amber") -> dict:
    ev = calendar_service().events().insert(calendarId=cal_id, body=body).execute()
    start = body["start"].get("dateTime") or body["start"].get("date")
    _log(con, actor, tier, "calendar.create", cal_name,
         f"added '{body.get('summary', '')}' on {start[:16]}", f"gcal:{cal_id}:{ev['id']}")
    return ev


def calendar_update(con, *, actor: str, cal_id: str, cal_name: str,
                    event_id: str, body: dict, tier: str = "amber") -> dict:
    ev = calendar_service().events().update(calendarId=cal_id, eventId=event_id,
                                            body=body).execute()
    _log(con, actor, tier, "calendar.update", cal_name,
         f"updated '{body.get('summary', '')}'", f"gcal:{cal_id}:{event_id}")
    return ev


def calendar_patch(con, *, actor: str, cal_id: str, cal_name: str,
                   event_id: str, patch: dict, what: str,
                   tier: str = "amber", quiet: bool = False) -> dict:
    """A partial update. `quiet` skips the audit row for identity-only
    patches (stamping a syncKey onto an event) that change nothing the user sees."""
    ev = calendar_service().events().patch(calendarId=cal_id, eventId=event_id,
                                           body=patch).execute()
    if not quiet:
        _log(con, actor, tier, "calendar.update", cal_name, what,
             f"gcal:{cal_id}:{event_id}")
    return ev


def calendar_cancel(con, *, actor: str, cal_id: str, cal_name: str,
                    existing: dict, sync_key: str, color_id: str = "8",
                    tier: str = "amber") -> dict | None:
    """Mark a managed event cancelled: retitle and grey, never delete.

    The stored hash is recomputed for the cancelled shape, so an event that
    later reappears in its source is restored cleanly instead of being treated
    as a hand edit and left saying CANCELLED forever.
    """
    if existing["summary"].startswith(CANCEL_PREFIX):
        return None
    since = existing.get("cancelled_since") or dt.date.today().isoformat()
    patch = {"summary": CANCEL_PREFIX + existing["summary"], "colorId": color_id}
    props = {"syncKey": sync_key, "cancelledSince": since,
             "syncHash": field_hash(_projected(existing, patch))}
    patch["extendedProperties"] = {"private": props}
    ev = calendar_service().events().patch(calendarId=cal_id, eventId=existing["id"],
                                           body=patch).execute()
    _log(con, actor, tier, "calendar.cancel", cal_name,
         f"marked '{existing['summary']}' cancelled", f"gcal:{cal_id}:{existing['id']}")
    return ev


def calendar_restore(con, *, actor: str, cal_id: str, cal_name: str,
                     existing: dict, color_id: str = "6",
                     tier: str = "amber") -> dict | None:
    """Undo calendar_cancel: drop the prefix and the grey. Used by repairs
    and by a sync when a source turns out not to have dropped the event."""
    if not existing["summary"].startswith(CANCEL_PREFIX):
        return None
    patch = {"summary": existing["summary"][len(CANCEL_PREFIX):], "colorId": color_id}
    props = {"syncKey": existing["syncKey"], "cancelledSince": "",
             "syncHash": field_hash(_projected(existing, patch))}
    patch["extendedProperties"] = {"private": props}
    ev = calendar_service().events().patch(calendarId=cal_id, eventId=existing["id"],
                                           body=patch).execute()
    _log(con, actor, tier, "calendar.restore", cal_name,
         f"restored '{patch['summary']}' (was marked cancelled)",
         f"gcal:{cal_id}:{existing['id']}")
    return ev


def calendar_delete_own(con, *, actor: str, cal_id: str, cal_name: str,
                        event_id: str, summary: str, tier: str = "amber") -> bool:
    """Delete an event, but only one Herald itself created.

    This is the one delete in the module and it exists for undo: on
    9 September 2026 a sync ran against half-migrated facts and created 51
    duplicates, and greying duplicates of Herald's own making would have left
    them cluttering the calendar for no one's benefit. The guard is the
    actions table: if no `calendar.create` row references the event, this
    refuses, because then it might be something the user made.
    """
    ref = f"gcal:{cal_id}:{event_id}"
    own = con.execute("SELECT 1 FROM actions WHERE kind='calendar.create' AND ref=?",
                      (ref,)).fetchone()
    if not own:
        return False
    calendar_service().events().delete(calendarId=cal_id, eventId=event_id).execute()
    _log(con, actor, tier, "calendar.delete", cal_name,
         f"deleted Herald's own duplicate '{summary}'", ref)
    return True


def calendar_delete_synced(con, *, actor: str, cal_id: str, cal_name: str,
                           event: dict, reason: str, key_prefixes: tuple[str, ...],
                           tier: str = "amber") -> bool:
    """Delete a scraper-written event the user has asked never to see again.

    The second delete in this module, and narrower than it sounds.
    `calendar_delete_own` is undo: events Herald itself created. This is the
    other case, raised on 9 September 2026 -- a machine-filled events calendar is a firehose Herald owns and keeps deterministically,
    and when the user rules a whole category off it, the ones already there have to
    go too or the rule only applies to the future. Greying them with
    CANCELLED would be a lie: they are not cancelled, they are unwanted, and
    a calendar full of false cancellations is worse than one with none.

    Three conditions, all required. The event must be **managed** -- it
    carries a syncHash, so a sync wrote it and no person did. It must be
    **unmodified** since that write, so the user has not edited it. And its
    syncKey must start with one of `key_prefixes`, which the caller sets to
    its own scraper sources; that is what keeps this away from `manual:`
    schedules and the `email:` events the scout cycle extracts. Anything
    failing a condition is refused, and the caller is expected to say so
    rather than pretend it went.
    """
    if not event.get("managed") or event.get("user_modified"):
        return False
    if not (event.get("syncKey") or "").startswith(key_prefixes):
        return False
    ref = f"gcal:{cal_id}:{event['id']}"
    calendar_service().events().delete(calendarId=cal_id, eventId=event["id"]).execute()
    _log(con, actor, tier, "calendar.delete", cal_name,
         f"removed '{event['summary']}' ({reason})", ref)
    return True


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------

def _tasklist_id(svc, name: str) -> str | None:
    resp = svc.tasklists().list(maxResults=100).execute()
    for tl in resp.get("items", []):
        if tl.get("title") == name:
            return tl["id"]
    return None


def task_create(con, *, actor: str, list_name: str, title: str,
                notes: str | None = None, due: str | None = None,
                tier: str = "amber") -> dict:
    """Add a task to one of the user's lists. `due` is a bare ISO date.

    The list must already exist: creating lists is their taxonomy, not ours.
    """
    svc = google.service("tasks", "v1")
    lid = _tasklist_id(svc, list_name)
    if not lid:
        raise ValueError(f"no Google Tasks list named {list_name!r}")
    body = {"title": title}
    if notes:
        body["notes"] = notes
    if due:
        body["due"] = f"{due[:10]}T00:00:00.000Z"
    task = svc.tasks().insert(tasklist=lid, body=body).execute()
    _log(con, actor, tier, "task.create", list_name,
         f"added task '{title}'" + (f" due {due[:10]}" if due else ""),
         f"gtask:{lid}:{task['id']}")
    return task


# --------------------------------------------------------------------------
# Gmail: labels and drafts. No sending.
# --------------------------------------------------------------------------

def gmail_service():
    return google.service("gmail", "v1")


def gmail_label_id(name: str, *, create: bool = False, con=None,
                   actor: str = "herald") -> str | None:
    svc = gmail_service()
    for lb in svc.users().labels().list(userId="me").execute().get("labels", []):
        if lb.get("name") == name:
            return lb["id"]
    if not create:
        return None
    lb = svc.users().labels().create(userId="me", body={
        "name": name, "labelListVisibility": "labelShow",
        "messageListVisibility": "show"}).execute()
    if con is not None:
        _log(con, actor, "amber", "gmail.label.create", name,
             f"created label '{name}'", f"gmail-label:{lb['id']}")
    return lb["id"]


def gmail_label(con, *, actor: str, message_id: str, label: str,
                remove: bool = False, subject: str = "",
                tier: str = "amber") -> dict:
    """Add (or remove) one label on one message. Creates the label if needed."""
    lid = gmail_label_id(label, create=not remove, con=con, actor=actor)
    if not lid:
        raise ValueError(f"no Gmail label named {label!r}")
    body = {"removeLabelIds": [lid]} if remove else {"addLabelIds": [lid]}
    msg = gmail_service().users().messages().modify(userId="me", id=message_id,
                                                    body=body).execute()
    verb = "removed" if remove else "applied"
    _log(con, actor, tier, "gmail.label", label,
         f"{verb} '{label}' on '{subject[:60]}'" if subject else f"{verb} '{label}'",
         f"gmail:{message_id}")
    return msg


def gmail_draft(con, *, actor: str, to: str, subject: str, body: str,
                thread_id: str | None = None, in_reply_to: str | None = None,
                cc: str | None = None, tier: str = "green") -> dict:
    """Create a draft in the user's Gmail. Green: nobody sees it until the user sends it."""
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload = {"message": {"raw": raw}}
    if thread_id:
        payload["message"]["threadId"] = thread_id
    draft = gmail_service().users().drafts().create(userId="me", body=payload).execute()
    _log(con, actor, tier, "gmail.draft", to,
         f"drafted '{subject[:60]}' to {to}", f"gmail-draft:{draft['id']}")
    return draft


# --------------------------------------------------------------------------
# Small shared helpers for syncs
# --------------------------------------------------------------------------

def norm_title(t: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())[:36]


def same_start(a: str | None, b: str | None) -> bool:
    """Compare two starts that may be a date or a datetime.

    An all-day event reads back as "2026-08-31" while a source may carry
    "2026-08-31T00:00:00-04:00"; comparing raw fails and the event gets
    duplicated on every run instead of adopted.
    """
    a, b = (a or ""), (b or "")
    if not a or not b:
        return False
    if len(a) == 10 or len(b) == 10:
        return a[:10] == b[:10]
    return a[:16] == b[:16]
