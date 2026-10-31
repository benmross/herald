#!/usr/bin/env python
"""Google Calendar — every calendar the user can see.

Reads a window around today rather than everything: the ledger is for what is
current, and history lives in Google. `singleEvents=True` expands recurrence so
a weekly lecture becomes the fifteen instances a query can reason about instead
of one rule a query cannot.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import collector, config, db, google  # noqa: E402

NAME = "gcal"
REQUIRES = ('google',)

# How often this is worth running:
# incremental through updatedMin
CADENCE_MINUTES = 30
PAST_DAYS = 30
# Far enough ahead that nothing real gets truncated. The old 120-day horizon
# was a cost control from before this collector was incremental, and it meant a
# May commencement or a spring registration date simply did not exist until it
# drifted inside the window -- then arrived as "new" when it had been on the
# calendar for months. updatedMin means a wider window costs nothing on a quiet
# pass, so there is no reason to keep the blinkers on.
FUTURE_DAYS = 500

# Re-reading 959 events every half hour to notice that none of them changed is
# exactly the waste this architecture is supposed to avoid. `updatedMin` asks
# Google for only what moved since the last run.
#
# The obvious tool would be a sync token, but Calendar refuses those alongside
# timeMin/timeMax, and the bounded window is worth more here than token-based
# sync: Herald wants the next four months, not the whole history of every
# calendar the user subscribes to. updatedMin gets the same delta inside that window.
#
# Overlap: clocks drift and Google's updated timestamps are not instantaneous,
# so re-ask for a few minutes either side of where we got to.
OVERLAP_MINUTES = 10


def _role(cal: dict) -> str:
    """What this calendar is evidence of.

    Google's accessRole is about permissions, not provenance. A machine-filled
    A machine-filled calendar comes back as "owner" because the user does own it
    -- their own scraper writes
    344 public events into it -- and a reader that trusts accessRole concludes
    the user is personally involved in every one of them. The user is not; that calendar is
    a firehose the user built.

    So the mapping is declared in config rather than inferred, and anything
    unrecognised is treated as a feed. Guessing "mine" invents commitments;
    guessing "feed" only loses some.
    """
    roles = config.get("calendars.roles", {}) or {}
    name = cal.get("summary") or ""
    # `calendars.ignore` outranks `roles`. It used to be honoured by one
    # section of one snapshot while the calendar still carried role "mine",
    # so every other reader -- a live session filtering on role, the
    # orientation card, the cycles' amber door -- treated a shared family
    # calendar as the user's own commitments. The user had to say so three
    # times. Stamping the role at the source is the only place every reader
    # already looks.
    ignored = set(config.get("calendars.ignore", []) or [])
    if name in ignored or cal.get("id") in ignored:
        return "ignored"
    if name in roles:
        return roles[name]
    if cal.get("id") in roles:
        return roles[cal["id"]]
    if cal.get("primary"):
        return "mine"
    return "feed"


def _iso(value: dict) -> tuple[str | None, bool]:
    """(timestamp, all_day) from a Calendar start/end block."""
    if not value:
        return None, False
    if "dateTime" in value:
        # Readers take the clock and date straight out of this string, and an
        # imported feed gives UTC, so a noon game read as 4:00 PM until stored
        # in the user's timezone.
        moment = dt.datetime.fromisoformat(value["dateTime"])
        return moment.astimezone(config.tz()).isoformat(), False
    return value.get("date"), True


def collect(con, *, reconcile: bool = False) -> dict:
    svc = google.service("calendar", "v3")
    now = dt.datetime.now(config.tz())
    lo = (now - dt.timedelta(days=PAST_DAYS)).isoformat()
    hi = (now + dt.timedelta(days=FUTURE_DAYS)).isoformat()

    since = collector.cursor(con, NAME)
    updated_min = None
    if since and not reconcile:
        try:
            updated_min = (dt.datetime.fromisoformat(since)
                           - dt.timedelta(minutes=OVERLAP_MINUTES))
        except (ValueError, TypeError):
            updated_min = None

    mode = "reconcile" if reconcile else "incremental" if updated_min else "full"
    counts = {"mode": mode, "calendars": 0, "events": 0, "removed": 0}
    skipped = 0

    # The set of calendars is a snapshot: one the user unsubscribes from should
    # disappear, not linger as a fact.

    page = None
    calendars = []
    while True:
        resp = svc.calendarList().list(pageToken=page, maxResults=250).execute()
        calendars.extend(resp.get("items", []))
        page = resp.get("nextPageToken")
        if not page:
            break

    # Finish network reads before taking SQLite's writer lock. A full repair
    # can span many pages and must not block conversations while it downloads.
    listings = {}
    incomplete = set()
    for cal in calendars:
        page, events = None, []
        while True:
            params = dict(calendarId=cal['id'], timeMin=lo, timeMax=hi,
                          singleEvents=True, orderBy='startTime',
                          maxResults=2500, pageToken=page)
            if reconcile:
                params['showDeleted'] = True
            if updated_min:
                params['updatedMin'] = updated_min.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
                params['showDeleted'] = True
            try:
                resp = svc.events().list(**params).execute()
            except Exception as error:
                print(f"  skipped {cal['id']}: {error}", file=sys.stderr)
                skipped += 1
                incomplete.add(cal['id'])
                break
            events.extend(resp.get('items', []))
            page = resp.get('nextPageToken')
            if not page:
                break
        listings[cal['id']] = events

    calendar_rows = []
    unclassified = []
    for cal in calendars:
        cal_id = cal["id"]
        role = _role(cal)
        declared = (cal.get("summary") in (config.get("calendars.roles", {}) or {})
                    or cal_id in (config.get("calendars.roles", {}) or {}))
        if not declared and not cal.get("primary"):
            unclassified.append(cal.get("summary") or cal_id)
        calendar_rows.append(dict(
            external_id=cal_id,
            title=cal.get("summary"),
            data={
                "role": role,
                "role_declared": declared,
                "access": cal.get("accessRole"),
                "primary": cal.get("primary", False),
                "selected": cal.get("selected", False),
                "timezone": cal.get("timeZone"),
                "writable": cal.get("accessRole") in ("owner", "writer"),
            },
        ))
        counts["calendars"] += 1

        listed_ids = set()
        complete_calendar = cal_id not in incomplete
        for ev in listings[cal_id]:
            listed_ids.add(f"{cal_id}::{ev['id']}")
            if ev.get("status") == "cancelled":
                # A cancellation is information. Drop the row rather than
                # leaving a meeting on the books that is not happening.
                counts["removed"] += con.execute(
                    "DELETE FROM facts WHERE source = ? AND kind = 'event'"
                    " AND external_id = ?",
                    (NAME, f"{cal_id}::{ev['id']}")).rowcount
                continue
            start, all_day = _iso(ev.get("start", {}))
            end, _ = _iso(ev.get("end", {}))
            db.put_fact(
                con, NAME, "event",
                external_id=f"{cal_id}::{ev['id']}",
                ts=start,
                title=ev.get("summary") or "(no title)",
                body=ev.get("description") or None,
                data={
                    "calendar_id": cal_id,
                    "calendar": cal.get("summary"),
                    # Denormalised so no query can forget to join for it.
                    "role": role,
                    "end": end,
                    "all_day": all_day,
                    "location": ev.get("location"),
                    "hangout": ev.get("hangoutLink"),
                    "attendees": [a.get("email") for a in ev.get("attendees", [])][:25],
                    "organizer": (ev.get("organizer") or {}).get("email"),
                    "status": ev.get("status"),
                    "recurring_event_id": ev.get("recurringEventId"),
                    "html_link": ev.get("htmlLink"),
                },
            )
            counts["events"] += 1
        if reconcile and complete_calendar:
            # A fully listed calendar is authoritative only inside its stated
            # window. Never prune another calendar, a failed page, or history
            # outside that window.
            stale = [r['id'] for r in con.execute(
                "SELECT id,external_id FROM facts WHERE source=? AND kind='event' "
                "AND json_extract(data,'$.calendar_id')=? AND ts>=? AND ts<?",
                (NAME, cal_id, lo, hi)) if r['external_id'] not in listed_ids]
            con.executemany('DELETE FROM facts WHERE id=?', [(fid,) for fid in stale])
            counts['removed'] += len(stale)

    db.replace_facts(con, NAME, 'calendar', calendar_rows)
    if unclassified:
        # A new calendar is being treated as a feed, which is the safe default
        # but may be wrong. Say so rather than silently deciding.
        counts["unclassified"] = len(unclassified)
        print(f"  calendars not in config/herald.json calendars.roles, "
              f"defaulting to 'feed': {', '.join(unclassified)}", file=sys.stderr)

    # The timestamp is shared by every calendar. Keep the previous one if
    # any listing failed, including a later page, so its changes are retried.
    counts["skipped calendars"] = skipped
    counts["_sync"] = {"complete": not skipped, "pending": skipped,
                       "reason": "calendar listings remain incomplete" if skipped else None,
                       "coverage": {"mode": mode, "start": lo, "end": hi,
                                    "calendars": counts["calendars"],
                                    "skipped": skipped}}
    counts["_cursor"] = None if skipped else now.isoformat(timespec="seconds")
    return counts


if __name__ == "__main__":
    collector.main(NAME, lambda con: collect(con, reconcile='--reconcile' in sys.argv))
