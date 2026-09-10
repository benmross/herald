#!/usr/bin/env python
"""Calendar feeds — any .ics URL, read into the ledger as facts.

A published calendar is the most widely available structured feed of what is
going to happen to somebody: a university's course deadlines, a team's fixture
list, a venue's programme, a shared family calendar. Almost every system that
holds dates will hand you one, and none of them need a token.

Herald's first version of this was Canvas-only, because its first user was a
student whose deadlines lived there. The parsing turned out to be generic --
identify the event, work out when it is, keep the description -- with exactly
one Canvas-shaped convention worth keeping, and that convention is common
enough to earn its place: many feeds suffix a summary with a bracketed context,
`Week 1: In-Class Activity [BIO201]`, and that bracket is the single most
useful field in the whole feed. It lands in `data.tag`.

Feeds are configured, not hardcoded:

    "calendar_feeds": [
      {"source": "canvas", "url_secret": "canvas.ics_url", "title": "..."},
      {"source": "orchestra", "url": "https://.../season.ics"}
    ]

`source` is the fact source these events are stored under, so a feed keeps its
own identity in the ledger and queries about it stay legible. A feed URL is
often a bearer credential in disguise -- anyone holding a Canvas .ics link can
read every due date in it -- so `url_secret` names a path in secrets.json and
`url` is for feeds that are genuinely public.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import collector, config, db  # noqa: E402

NAME = "calendar_feeds"
REQUIRES = ("calendar_feeds",)

# How often this is worth running:
# a published feed changes when someone edits it, not hourly
CADENCE_MINUTES = 180

# "Week 1: In-Class Activity [BIO201]" -> title, tag
_SUFFIX = re.compile(r"^(?P<title>.*?)\s*\[(?P<tag>[^\]]+)\]\s*$")
_CONTEXT_ID = re.compile(r"include_contexts=\w+?_(\d+)")

# Canvas ships a full HTML rendering of every assignment alongside the plain
# text one. It is many kilobytes of stylesheet-laden markup per event and we
# have no use for it.
_DESCRIPTION_CAP = 4000


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "herald/0.1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _kind(uid: str) -> str:
    """Feeds that distinguish a deadline from an appointment say so in the UID.

    Worth keeping generic: a thing that is *due* and a thing that *happens* are
    different to a person, and a digest that cannot tell them apart reads every
    deadline as an appointment nobody has to attend.
    """
    lowered = uid.lower()
    if "assignment" in lowered or "due" in lowered:
        return "assignment"
    if "syllabus" in lowered:
        return "syllabus"
    return "event"


def _when(component) -> tuple[str | None, bool]:
    """ISO timestamp and whether it was an all-day entry.

    An all-day DTSTART on a deadline means "due that day" rather than "happens
    at midnight", so the distinction has to survive into the ledger or every
    deadline reads as 00:00.
    """
    raw = component.get("DTSTART")
    if raw is None:
        return None, False
    value = raw.dt
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(config.tz()).isoformat(timespec="seconds"), False
    return value.isoformat(), True


def _feed_url(feed: dict) -> str:
    if feed.get("url_secret"):
        url = config.secret(feed["url_secret"])
        if not url:
            raise RuntimeError(f"secrets.json has no {feed['url_secret']}")
        return url
    if feed.get("url"):
        return feed["url"]
    raise RuntimeError(f"calendar feed {feed.get('source')!r} has neither url nor url_secret")


def _read_feed(con, feed: dict) -> dict:
    from icalendar import Calendar

    source = feed.get("source")
    if not source:
        raise RuntimeError("every calendar feed needs a `source` name")
    raw = _fetch(_feed_url(feed))

    # Keep the feed so a parser change can be re-run without re-fetching.
    snapshot = config.RAW / "calendar_feeds"
    snapshot.mkdir(parents=True, exist_ok=True)
    (snapshot / f"{source}.ics").write_bytes(raw)

    cal = Calendar.from_ical(raw)
    counts = {"assignments": 0, "events": 0, "tags": 0}
    tags: dict[str, dict] = {}

    for comp in cal.walk("VEVENT"):
        uid = str(comp.get("UID") or "").strip()
        if not uid:
            continue

        summary = str(comp.get("SUMMARY") or "").strip()
        title, tag = summary, None
        if m := _SUFFIX.match(summary):
            title, tag = m.group("title").strip(), m.group("tag").strip()

        url_prop = str(comp.get("URL") or "")
        context_id = m2.group(1) if (m2 := _CONTEXT_ID.search(url_prop)) else None

        ts, all_day = _when(comp)
        description = str(comp.get("DESCRIPTION") or "").strip()
        kind = _kind(uid)

        db.put_fact(
            con, source, kind,
            external_id=uid,
            ts=ts,
            title=title,
            body=description[:_DESCRIPTION_CAP] or None,
            data={
                "tag": tag,
                "context_id": context_id,
                "location": str(comp.get("LOCATION") or "") or None,
                "url": url_prop or None,
                "all_day": all_day,
                "summary": summary,
                "truncated": len(description) > _DESCRIPTION_CAP,
            },
        )
        counts["assignments" if kind == "assignment" else "events"] += 1

        if tag and tag not in tags:
            tags[tag] = {"tag": tag, "context_id": context_id, "feed": source}

    # A distinct fact per tag, so "what is in this feed" is a query rather than
    # a scan over every event. Derived from the feed, so rebuilt rather than
    # upserted -- a tag that stops appearing has to stop existing.
    db.clear(con, source, "tag")
    for tag, info in tags.items():
        db.put_fact(con, source, "tag", external_id=tag, title=tag, data=info)
    counts["tags"] = len(tags)
    return counts


def collect(con) -> dict:
    feeds = config.get("calendar_feeds") or []
    totals: dict[str, int] = {}
    failures: list[str] = []
    for feed in feeds:
        try:
            for key, value in _read_feed(con, feed).items():
                totals[f"{feed.get('source', '?')} {key}"] = value
            # Commit per feed, because the failure below re-raises and
            # collector.run rolls back on the way out: without this, one dead
            # feed would silently discard every healthy feed's work in the
            # same pass.
            con.commit()
        except Exception as exc:                                    # noqa: BLE001
            failures.append(f"{feed.get('source', '?')}: {type(exc).__name__}: {exc}")
    if failures:
        raise RuntimeError("; ".join(failures))
    return totals


if __name__ == "__main__":
    collector.main(NAME, collect)
