#!/usr/bin/env python
"""Build the brief a cycle reasons over.

This is the deterministic half of a cycle and it is where the token budget is
actually decided. The agent gets one compact document instead of a database and
an invitation to go fishing: every row here was chosen by a query, so a cycle
costs roughly the same whether the ledger holds two thousand facts or two
hundred thousand.

Everything here is a plain SQL read. No judgment, no model.

**It queries by shape, not by source.** The first version named its sources:
one service for deadlines, another for messages, another for location, which
meant the program knew the name of every service one particular person happened
to use. Now a deadline is
any fact of kind `assignment`, a conversation is any `thread`, a position is any
`current`, wherever it came from. A collector that writes the right shape shows
up in the brief without the program having heard of it.

What cannot be generalised is contributed by whoever owns it: an
extension may ship a `snapshot.py` with a `section(con, ctx)` function, and its
output lands in the brief in `ORDER` position. Grades and announcements from a
university's Canvas belong there, not here.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import pathlib
import re
import sys
from collections import Counter
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import changes, config, db, deadlines, extensions, mailreadings, mailtext  # noqa: E402

# Near-term detail, then everything else in brief. Nothing is hidden merely for
# being distant: an exam eight weeks out still belongs in the picture, it just
# does not need a line of its own every morning.
DUE_HORIZON_DAYS = 21
SCHEDULE_DAYS = 2
AWAITING_LIMIT = 20
NEW_MAIL_LIMIT = 40
# Text shown for mail no session has read yet. Direct mail arrives whole unless
# it is enormous; bulk mail gets enough to find its point. A budget bounds a
# morning with a large backlog, and what it holds back is named, never guessed at.
UNREAD_DIRECT_CHARS = 6_000
UNREAD_BULK_CHARS = 1_500
UNREAD_TOTAL_CHARS = 40_000
# Google's replies to an invitation. They report one guest's answer and say
# nothing about whether the event is still on.
CALENDAR_REPLY = re.compile(r"^(Accepted|Declined|Tentative|Tentatively accepted|Maybe)\s*:", re.I)

WHO = config.person()


def _rows(con, sql: str, params=()) -> list:
    return con.execute(sql, params).fetchall()


def _when_words(date_str: str, today: dt.date) -> str:
    """"Wed 9 Sep (in 3 days)" for a bare date.

    The agent should never be computing weekday names or day counts from an ISO
    string: it is arithmetic, it is free here, and getting it wrong is both easy
    and load-bearing. The first digest called 9 September a Tuesday.
    """
    try:
        d = dt.date.fromisoformat(date_str[:10])
    except (ValueError, TypeError):
        return date_str
    delta = (d - today).days
    if delta == 0:
        rel = "TODAY"
    elif delta == 1:
        rel = "tomorrow"
    elif delta < 0:
        rel = f"{-delta}d ago"
    else:
        rel = f"in {delta}d"
    return f"{d:%a %-d %b} ({rel})"


def _j(row, key: str, default=None):
    try:
        return json.loads(row["data"] or "{}").get(key, default)
    except (json.JSONDecodeError, TypeError, KeyError):
        return default


def _fix_age(row) -> int | None:
    """Minutes since a `current` fact's fix, from its timestamp.

    A source's own `age_minutes` is how old the fix was when it was stored,
    which for a location shared once on Telegram is zero forever.
    """
    try:
        when = dt.datetime.fromisoformat(row["ts"])
        if when.tzinfo is None:
            when = when.replace(tzinfo=config.tz())
        return max(0, round((dt.datetime.now(config.tz()) - when).total_seconds() / 60))
    except (TypeError, ValueError):
        return _j(row, "age_minutes")


def last_cycle(con, label: str) -> str | None:
    row = con.execute(
        "SELECT MAX(ts) t FROM runs WHERE label = ? AND exit_code = 0", (label,)
    ).fetchone()
    return row["t"] if row and row["t"] else None


@dataclass
class Context:
    """What an extension's snapshot section is handed.

    Deliberately small: the connection, the clock, and the two helpers whose
    absence produces bad briefs (a date the agent has to do arithmetic on, and
    a NEW marker that has to agree with the `reported` table).
    """
    con: object
    cycle: str
    now: dt.datetime
    today: dt.date
    since: str | None
    flag: object
    when_words: object
    who: dict

    def rows(self, sql: str, params=()) -> list:
        return self.con.execute(sql, params).fetchall()

    def value(self, row, key: str, default=None):
        return _j(row, key, default)


def _extension_sections(ctx: Context) -> list[tuple[int, str]]:
    """Sections contributed by enabled extensions, with their order.

    Loaded by path rather than by import name so two extensions may both call
    their module `snapshot`, and wrapped so a broken extension costs its own
    section rather than the whole brief -- a cycle that cannot build a snapshot
    tells the user nothing at all.
    """
    out: list[tuple[int, str]] = []
    for ext in extensions.enabled():
        path = ext.path / "snapshot.py"
        if not path.exists():
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                f"herald_ext_{ext.name}_snapshot", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            text = module.section(ctx.con, ctx)
        except Exception as exc:                                    # noqa: BLE001
            print(f"snapshot: {ext.name} section failed: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        if text:
            out.append((getattr(module, "ORDER", 50), text.rstrip()))
    return out


def _event_time(value) -> str:
    try:
        when = dt.datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return str(value)
    if len(value) <= 10:
        return f"{when:%a %-d %b}"
    return f"{when.astimezone(config.tz()):%a %-d %b}, {config.clock(when.astimezone(config.tz()))}"


def _disputed_dates(con, today: dt.date) -> list[str]:
    """Live dates that recent mail suggests have moved, however the row reads."""
    horizon = (today + dt.timedelta(days=DUE_HORIZON_DAYS)).isoformat()
    rows = _rows(con, "SELECT id,title,course,due FROM deadlines WHERE status='live' "
                      "AND substr(due,1,10)>=? AND substr(due,1,10)<=? ORDER BY due",
                 (today.isoformat(), horizon))
    found = deadlines.disputes(con, rows)
    return [f"- {r['title']}, recorded as {_when_words(str(r['due'])[:10], today)}: recent mail "
            f"“{_clip(found[r['id']], 90)}”. Read it before stating this date."
            for r in rows if r["id"] in found]


def _calendar_changes(con, since, today: dt.date) -> list[str]:
    """Own events removed, moved or renamed since the last cycle.

    A cancellation leaves no row behind, so a brief built only from what is on
    the calendar cannot say one happened, and mail about the event was left
    speaking for it. On 30 October 2026 that put a meeting cancelled the evening
    before back in the digest, on the strength of a guest's acceptance.
    """
    if not since:
        return []
    moment = dt.datetime.fromisoformat(since)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=config.tz())
    stamp = moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    first = con.execute("SELECT MIN(seq) FROM ledger_changes WHERE changed_at>?", (stamp,)).fetchone()[0]
    if first is None:
        return []
    lines = []
    for r in changes.net(con, first - 1):
        before, after = r["before"], r["after"]
        if r["entity"] != "facts" or before is None:
            continue
        old_data = before.get("data") if isinstance(before.get("data"), dict) else {}
        if before.get("kind") != "event" or old_data.get("role") != "mine":
            continue
        if str(before.get("ts"))[:10] < today.isoformat():
            continue
        title, when = _clip(before.get("title"), 70), _event_time(before.get("ts"))
        new_data = (after or {}).get("data") if isinstance((after or {}).get("data"), dict) else {}
        if after is None or new_data.get("status") == "cancelled":
            lines.append(f"- CANCELLED or removed: {title}, was {when}.")
        elif after.get("ts") != before.get("ts"):
            lines.append(f"- MOVED: {title}, from {when} to {_event_time(after.get('ts'))}.")
        elif after.get("title") != before.get("title"):
            lines.append(f"- RENAMED: {title} is now {_clip(after.get('title'), 70)}, {when}.")
        elif new_data.get("location") != old_data.get("location"):
            lines.append(f"- NEW ROOM: {title}, {when}: now {_clip(new_data.get('location'), 60)}.")
    return lines[:15]


def build(con, cycle: str) -> tuple[str, set[str]]:
    """The brief, and the set of references it presented as new.

    The caller records those once the digest actually goes out, so nothing is
    marked as told unless the user was told.
    """
    now = dt.datetime.now(config.tz())
    today = now.date()
    since = last_cycle(con, f"cycle:{cycle}")
    seen = db.already_reported(con)
    shown: set[str] = set()
    out: list[str] = []
    name, they, them, their = WHO["first"], WHO["subject"], WHO["object"], WHO["possessive"]

    def flag(ref: str) -> str:
        """"NEW" the first time something appears in a digest, blank after."""
        shown.add(ref)
        return "" if ref in seen else "  **NEW**"

    def head(text: str) -> None:
        out.append(f"\n## {text}")

    health = changes.health(con)
    if health:
        head("Incomplete source coverage")
        out.extend(changes.health_lines(health))

    ctx = Context(con=con, cycle=cycle, now=now, today=today, since=since,
                  flag=flag, when_words=_when_words, who=WHO)
    ext_sections = _extension_sections(ctx)

    def pour(before: int) -> None:
        """Emit any extension section that sorts before this point."""
        remaining = []
        for order, text in ext_sections:
            if order < before:
                out.append("\n" + text)
            else:
                remaining.append((order, text))
        ext_sections[:] = remaining

    out.append(f"# Snapshot — {cycle} cycle — {now:%A %-d %B %Y}, {config.clock(now)} {now:%Z}")
    if str(config.get("time_format", "24h")).lower() == "12h":
        out.append("\nWrite every clock time in 12-hour form (2:30 PM), including "
                   "times copied from mail or listings.")
    if since:
        out.append(f"\nLast {cycle} cycle: {since}")
    else:
        out.append(f"\nNo previous {cycle} cycle — this is the first one.")

    # --- where they are ----------------------------------------------------
    # Any source that keeps a `current` fact: a phone, a location server, a
    # check-in. The program does not need to know which.
    loc = _rows(con, "SELECT * FROM facts WHERE kind='current' ORDER BY ts DESC LIMIT 1")
    if loc:
        head(f"Where {they} {'are' if WHO['plural_verb'] else 'is'}")
        r = loc[0]
        age = _fix_age(r)
        line = f"{r['title']} — last fix {age} min ago" if age is not None else str(r["title"])
        if _j(r, "city_is_stale"):
            line += (f" (the newest fix is not geocoded yet; the place name is from"
                     f" {_j(r, 'city_age_minutes')} min ago)")
        out.append(line)

    # --- what Herald itself did since the last digest ------------------------
    # The amber tier is "never silent". gwrite logs every write; this is where
    # they get read out. Grouped, because one calendar pass can legitimately
    # write two hundred events and nobody wants two hundred lines.
    acts = db.unreported_actions(con)
    if acts:
        head(f"What Herald did on {their} behalf since the last digest "
             f"({len(acts)} actions)")
        out.append("These already happened. Report them in one or two lines each "
                   f"group; they are amber actions and {they} must hear about "
                   f"them, but they are not news {they} must act on.")
        groups: dict[tuple, list] = {}
        for a in acts:
            groups.setdefault((a["actor"], a["kind"], a["target"]), []).append(a)
        for (actor, kind, target), rows in groups.items():
            out.append(f"- {actor} · {kind} · {target}: {len(rows)}")
            for a in rows[:4]:
                out.append(f"    - {config.clock(a['ts'][11:16])} {a['summary']}")
            if len(rows) > 4:
                out.append(f"    - …and {len(rows) - 4} more")

    # A waiting release, and any red action waiting on a tap. Both are things
    # the user has to answer rather than things that happened, so they come
    # before the day itself.
    release = _rows(con, "SELECT * FROM facts WHERE source='upstream' "
                         "AND kind='release' ORDER BY ts DESC LIMIT 1")
    if release:
        r = release[0]
        head("An update to Herald is waiting")
        out.append(f"{r['title']} — {_j(r, 'commits')} commit(s) since "
                   f"{_j(r, 'from') or 'this install'}. It is offered as a "
                   f"one-tap approval; mention it once, briefly, and do not "
                   f"repeat it every morning."
                   + flag(f"upstream:{r['external_id']}"))
        if r["body"]:
            out.append(f"    {r['body'][:400].replace(chr(10), ' ')}")

    try:
        from herald import ledger  # noqa: PLC0415
        stuck = ledger.backlog()
    except Exception:                                               # noqa: BLE001
        stuck = None
    if stuck and stuck[1] >= ledger.BACKLOG_HOURS:
        head("The ledger's backup is not reaching its remote")
        why = os.environ.get("HERALD_LEDGER_PUSH_ERROR")
        out.append(f"{stuck[0]} commit(s) in $HERALD_HOME have not been pushed, the "
                   f"oldest {stuck[1] / 24:.1f} days ago. "
                   + (f"This cycle's push just failed: {why}. " if why else
                      "`git -C $HERALD_HOME push` says why. ")
                   + f"Tell {them}: the only copy is on this machine.")

    waiting = _rows(con, "SELECT * FROM approvals WHERE state='pending' ORDER BY ts")
    if waiting:
        head(f"Waiting on a yes or no from {them} ({len(waiting)})")
        out.append("These were asked as one-tap questions and never answered. "
                   "Worth one line if any still matters; drop the ones that "
                   "have gone stale.")
        for a in waiting:
            out.append(f"- {(a['ts'] or '')[:16].replace('T', ' ')}  "
                       f"{a['kind']}: {a['summary']}")

    pour(20)          # extensions that want to be near the top

    # --- schedule ----------------------------------------------------------
    ignored = set(config.get("calendars.ignore", []) or [])
    for offset in range(SCHEDULE_DAYS):
        day = today + dt.timedelta(days=offset)
        label = {0: "Today", 1: "Tomorrow"}.get(offset, day.strftime("%A"))
        head(f"{label} — {day:%A %-d %B %Y}")
        evs = _rows(con, """
            SELECT * FROM facts
            WHERE source='gcal' AND kind='event' AND substr(ts,1,10) = ?
            ORDER BY ts
        """, (day.isoformat(),))
        evs = [e for e in evs if _j(e, "calendar") not in ignored
               and _j(e, "role") != "ignored"]

        # Separated by provenance, because mixing them is how a machine-filled
        # firehose calendar gets read as the user's schedule -- or worse, as
        # evidence that they belong to something. `role` comes from
        # calendars.roles in the config and is the whole point of that setting.
        buckets = {"mine": [], "other-person": [], "reference": [], "feed": []}
        for e in evs:
            buckets.setdefault(_j(e, "role") or "feed", buckets["feed"]).append(e)

        def fmt(e):
            when = config.clock(e["ts"][11:16]) if len(e["ts"] or "") > 11 else "all day"
            where = _j(e, "location") or ""
            cal = _j(e, "calendar") or ""
            return (f"- {when}  {e['title']}"
                    + (f" — {where}" if where else "")
                    + (f"  [{cal}]" if cal else "")
                    + flag(f"gcal:{e['external_id']}"))

        if not buckets["mine"]:
            out.append(f"**{their.capitalize()} own commitments:** nothing scheduled")
        else:
            out.append(f"**{their.capitalize()} own commitments:**")
            out.extend(fmt(e) for e in buckets["mine"])

        for role, label in (("reference", "Reference calendars (holidays, institutional)"),
                            ("other-person", "Someone else's calendar, not theirs")):
            if buckets[role]:
                out.append(f"\n**{label}:**")
                out.extend(fmt(e) for e in buckets[role])

        if buckets["feed"]:
            out.append(f"\n**Public events {they} COULD go to "
                       f"({len(buckets['feed'])}) — not commitments, and not "
                       f"evidence {they} {'are' if WHO['plural_verb'] else 'is'} "
                       f"involved in anything:**")
            out.extend(fmt(e) for e in buckets["feed"][:12])
            if len(buckets["feed"]) > 12:
                out.append(f"  …and {len(buckets['feed']) - 12} more")

    # --- deadlines ---------------------------------------------------------
    # Any fact of kind `assignment`, from any source: a course feed, a client's
    # tracker, a form with a closing date.
    head(f"Due in the next {DUE_HORIZON_DAYS} days")
    horizon = (today + dt.timedelta(days=DUE_HORIZON_DAYS)).isoformat()
    due = _rows(con, """
        SELECT * FROM facts
        WHERE kind='assignment' AND substr(ts,1,10) >= ? AND substr(ts,1,10) <= ?
        ORDER BY ts
    """, (today.isoformat(), horizon))
    # Two sources can carry the same deadline -- a course's public feed and the
    # same course read through an authenticated session -- and a digest that
    # lists an exam twice is a digest that gets skimmed. Same title, same day,
    # one line; the row that knows about submission state wins, because that is
    # the one that can say it is already handed in.
    def _richer(row) -> int:
        return 1 if _j(row, "submission") else 0

    unique: dict[tuple, object] = {}
    for a in due:
        key = ((a["title"] or "").strip().casefold(), (a["ts"] or "")[:10])
        if key not in unique or _richer(a) > _richer(unique[key]):
            unique[key] = a
    due = sorted(unique.values(), key=lambda r: r["ts"] or "")

    if not due:
        out.append("nothing with a due date")
    for a in due:
        words = _when_words(a["ts"], today)
        clock = config.clock(a["ts"][11:16]) if len(a["ts"] or "") > 11 else ""
        tag = _j(a, "tag") or _j(a, "course") or a["source"]
        out.append(f"- {words}{' ' + clock if clock else ''}  [{tag}]  {a['title']}"
                   + flag(f"{a['source']}:{a['external_id']}"))

    beyond = _rows(con, """
        SELECT * FROM facts
        WHERE kind='assignment' AND substr(ts,1,10) > ? ORDER BY ts
    """, (horizon,))
    if beyond:
        out.append(f"\nFurther out ({len(beyond)}): "
                   + "; ".join(f"{_when_words(a['ts'], today)} {a['title'][:34]}"
                               for a in beyond[:6]))
        if len(beyond) > 6:
            out.append(f"  …and {len(beyond) - 6} more beyond that.")

    tags = _rows(con, "SELECT DISTINCT title FROM facts WHERE kind='tag' ORDER BY title")
    if tags:
        out.append(f"\n(Deadline feeds cover: {', '.join(t['title'] for t in tags)}. "
                   f"Anything not in that list publishes nothing here, so silence "
                   f"about it is not evidence there is nothing due.)")

    pour(60)

    # --- open loops --------------------------------------------------------
    head(f"Waiting on a reply from {them} (mail)")
    waiting = _rows(con, """
        SELECT * FROM facts
        WHERE source='gmail' AND kind='thread'
          AND json_extract(data,'$.awaiting_reply') = 1
        ORDER BY ts DESC LIMIT ?
    """, (AWAITING_LIMIT,))
    if not waiting:
        out.append("nobody")
    for t in waiting:
        out.append(f"- {(t['ts'] or '')[:10]}  {_j(t, 'last_from')}  —  {t['title']}")

    # Messages, from whatever writes them. A source that knows it is stale says
    # so in a `health` fact, and a stale list is suppressed rather than shown
    # wrong: "nobody is waiting" from a sync that stopped two days ago is worse
    # than saying nothing.
    stale = [h for h in _rows(con, "SELECT * FROM facts WHERE kind='health'")
             if _j(h, "stale")]
    head(f"Waiting on a message back from {them}")
    if stale:
        for h in stale:
            out.append(f"{h['source']} is {_j(h, 'hours_behind')}h behind, so this "
                       f"list is suppressed rather than wrong. Do not claim "
                       f"nobody is waiting.")
    else:
        texts = _rows(con, """
            SELECT * FROM facts
            WHERE kind='thread' AND source <> 'gmail'
              AND json_extract(data,'$.awaiting_reply') = 1
            ORDER BY ts DESC LIMIT 12
        """)
        if not texts:
            out.append("nobody")
        for t in texts:
            days = _j(t, "days_since")
            if days is None and t["ts"]:
                days = round((now - dt.datetime.fromisoformat(t["ts"])).total_seconds() / 86_400, 1)
            body = (t["body"] or "").replace("\n", " ")[:110]
            out.append(f"- {t['title']}"
                       + (f" ({days}d ago)" if days is not None else "")
                       + f": “{body}”")

    head("Live opportunities")
    opps = _rows(con, """
        SELECT * FROM opportunities WHERE status IN ('new','surfaced','interested')
        ORDER BY COALESCE(deadline,'9999') LIMIT 12
    """)
    if not opps:
        out.append("none tracked yet")
    for o in opps:
        when = _when_words(o["deadline"], today) if o["deadline"] else "no deadline"
        out.append(f"- #{o['id']} [{o['status']}] {when}  {o['title']}"
                   f" ({o['org'] or '?'})  score {o['score']}"
                   + flag(f"opp:{o['id']}"))

    head("Open commitments")
    commits = _rows(con, "SELECT * FROM commitments WHERE status='open' ORDER BY due")
    if not commits:
        out.append("none tracked yet")
    for c in commits:
        due_words = _when_words(c["due"], today) if c["due"] else "no date"
        out.append(f"- #{c['id']}  {due_words}  ({c['kind'] or 'note'})  {c['text']}"
                   + flag(f"commitment:{c['id']}"))

    tasks = _rows(con, """
        SELECT * FROM facts WHERE source='gtasks' AND kind='task'
          AND json_extract(data,'$.status') = 'needsAction'
        ORDER BY COALESCE(ts, '9999') LIMIT 20
    """)
    if tasks:
        head("Open tasks")
        for t in tasks:
            due_words = _when_words(t["ts"], today) if t["ts"] else "no date"
            out.append(f"- {due_words}  {t['title']}  [{_j(t, 'list')}]")

    # --- what changed ------------------------------------------------------
    moved = _calendar_changes(con, since, today)
    if moved:
        head("Calendar changes since the last cycle")
        out.append("These come from the calendar, which outranks any mail about "
                   "the same event.")
        out.extend(moved)
    disputed = _disputed_dates(con, today)
    if disputed:
        head("Dates that recent mail may have changed")
        out.extend(disputed)

    head("New mail since the last cycle")
    if since:
        fresh = _rows(con, """
            SELECT * FROM facts
            WHERE source IN ('gmail', 'purelymail') AND kind='message' AND ts > ?
              AND json_extract(data,'$.from_me') = 0
            ORDER BY ts DESC LIMIT ?
        """, (since, NEW_MAIL_LIMIT))
    else:
        fresh = _rows(con, """
            SELECT * FROM facts
            WHERE source IN ('gmail', 'purelymail') AND kind='message'
              AND json_extract(data,'$.from_me') = 0
            ORDER BY ts DESC LIMIT ?
        """, (NEW_MAIL_LIMIT,))
    if not fresh:
        out.append("nothing new")
    readings = mailreadings.lookup(con, fresh)
    budget = UNREAD_TOTAL_CHARS
    for m in fresh:
        mark = "bulk" if _j(m, "bulk") else "direct"
        out.append(f"- [{mark}] {(m['ts'] or '')[:16].replace('T', ' ')}  "
                   f"{_j(m, 'from')}  —  {m['title']}")
        reading = readings.get(mailreadings.key(m))
        if reading:
            out.append(f"      prior text interpretation (attachments not covered): {_clip(reading['says'], 320)}")
            continue
        if CALENDAR_REPLY.match(m["title"] or ""):
            out.append("      a guest's reply to an invitation; it says nothing about "
                       "whether the event is on")
            continue
        text = mailtext.compact(m["body"] or "") or (_j(m, "snippet") or "").strip()
        cap = min(UNREAD_BULK_CHARS if _j(m, "bulk") else UNREAD_DIRECT_CHARS, budget)
        token = mailreadings.reading_id(m, complete=bool(text) and len(text) <= cap)
        if token:
            out.append(f"      reading id: {token}")
        if not text:
            out.append("      no text; the message may be images only")
        elif cap <= 0:
            out.append(f"      text held back by this brief's budget: `herald fact {m['id']}`")
        else:
            budget -= min(cap, len(text))
            out.append("      <<<")
            out.append(text[:cap])
            out.append("      >>>")
            if len(text) > cap:
                out.append(f"      {cap} of {len(text)} characters; the rest: `herald fact {m['id']}`")

    gh = _rows(con, """
        SELECT * FROM facts WHERE source='github' AND kind='event'
        ORDER BY ts DESC LIMIT 6
    """)
    if gh:
        head(f"What {they} {'have' if WHO['plural_verb'] else 'has'} been building")
        for g in gh:
            out.append(f"- {(g['ts'] or '')[:10]}  {g['title']}")

    pour(10_000)      # anything that asked to come last

    out.append("\n---\n**NEW** marks something that has never appeared in a "
               f"digest before. Everything unmarked, {they} "
               f"{'have' if WHO['plural_verb'] else 'has'} already been told at "
               "least once — repeat it only if it changed, or if it is close "
               "enough now that the timing itself is the news.")
    return "\n".join(out), shown


# --- orientation: the same idea, for a conversation rather than a cycle -------
#
# Session facts avoid repeated discovery. Retrieval instructions live in the
# constitution and herald-ledger skill; do not duplicate inventories here.

ORIENTATION_DUE_DAYS = 10
ORIENTATION_LIMIT = 8


def orientation(con) -> str:
    """Session-start facts only; shared instructions carry retrieval guidance."""
    now = dt.datetime.now(config.tz())
    today = now.date().isoformat()
    horizon = (now.date() + dt.timedelta(days=ORIENTATION_DUE_DAYS)).isoformat()
    out = [f"# Orientation: {now:%A %-d %B %Y}, {config.clock(now)} {now:%Z}",
           "Snapshot at session start; verify relevant sources before acting."]

    health = changes.health(con)
    if health:
        out.append("\nIncomplete source coverage:")
        out.extend(changes.health_lines(health))
    loc = _rows(con, "SELECT * FROM facts WHERE kind='current' ORDER BY ts DESC LIMIT 1")
    if loc:
        age = _fix_age(loc[0])
        suffix = f" (last fix {age} min ago)" if age is not None else ""
        out.append(f"\nLocation: {loc[0]['title']}{suffix}")

    # Include ongoing events, exclude ended/cancelled ones and other calendars.
    sched = _rows(con, """
        SELECT title, ts, data FROM facts
        WHERE kind='event' AND substr(ts,1,10)=?
          AND json_extract(data,'$.role')='mine'
          AND COALESCE(json_extract(data,'$.status'),'') NOT IN ('cancelled','canceled')
          AND title NOT LIKE 'CANCELLED%' AND title NOT LIKE 'CANCELED%'
          AND (json_extract(data,'$.all_day')=1 OR length(ts)=10
               OR julianday(COALESCE(json_extract(data,'$.end'),ts))>julianday(?))
        ORDER BY ts
    """, (today, now.isoformat()))
    if sched:
        out.append(f"\nRemaining today (own calendar, first {ORIENTATION_LIMIT} at most):")
        for r in sched[:ORIENTATION_LIMIT]:
            when = ("all day" if _j(r, "all_day") or len(r["ts"]) == 10
                    else config.clock(str(r["ts"])[11:16]))
            out.append(f"- {when} {r['title']}")
        if len(sched) > ORIENTATION_LIMIT:
            out.append(f"{len(sched) - ORIENTATION_LIMIT} more; use `herald context --view schedule`.")

    # Obligations can be overdue indefinitely. Count them separately, rather
    # than mixing them into an upcoming-dates list that hides actual deadlines.
    due = _rows(con, """
        SELECT title, kind, due FROM deadlines
        WHERE status='live' AND substr(due,1,10) BETWEEN ? AND ? ORDER BY due
    """, (today, horizon))
    if due:
        out.append(f"\nNext {ORIENTATION_DUE_DAYS} days ({len(due)} live dates):")
        for r in due[:ORIENTATION_LIMIT]:
            out.append(f"- {_when_words(str(r['due'])[:10], now.date())}: {r['title']} ({r['kind']})")
        if len(due) > ORIENTATION_LIMIT:
            out.append("More: `herald context --view deadlines`.")
    disputed = _disputed_dates(con, now.date())
    if disputed:
        out.append("\nVerify changed dates before citing:")
        out.extend(disputed)
    obligations = con.execute("""
        SELECT COUNT(*) n, COALESCE(SUM(due IS NOT NULL AND substr(due,1,10)<?),0) overdue
        FROM commitments WHERE status='open'
    """, (today,)).fetchone()
    if obligations["n"]:
        out.append(f"\nOpen obligations: {obligations['n']} ({obligations['overdue']} overdue). "
                   "Retrieve with `herald context --view commitments` when relevant.")
    return "\n".join(out)


# --- what changed since the last message ------------------------------------
#
# The orientation card above is built once per session and then deliberately
# frozen, because it lives in the system prompt and the system prompt is the
# front of the cached prefix: rebuilding it every turn would invalidate the
# whole conversation's cache on every message.
#
# That leaves a real gap, pointed out by the user on 12 Sep 2026. A conversation can
# run for hours. Collectors fire at :02 and :32, cycles write to the calendar,
# mail arrives, a deadline passes. Mid-conversation the session knew none of it
# unless it thought to go and look, which it had no reason to do.
#
# So the delta goes in the *user message* instead, which is new tokens either
# way and sits after everything cached. It costs nothing in cache terms and a
# few dozen tokens in real ones.
#
# Two rules it lives by. **It reports rows, not text.** A textual diff of the
# card would fire on every single turn, because the card carries a generation
# timestamp and a "last fix N minutes ago". Both always change and
# neither is news. Substantive record revisions distinguish in-place edits
# from unchanged refreshes. **It returns nothing when nothing happened**, the
# same test the rest of the architecture is held to: a turn where the world did
# not move should cost nothing to tell you so.

DELTA_ITEM_LIMIT = 6
DELTA_RECORD_LIMIT = 12
# Bulk mail is usually noise, but a course system's "due date changed" is bulk
# too. The subject line decides whether it earns a line of its own.
MAIL_SIGNAL = re.compile(r"due date|deadline|cancel|reschedul|postpone|moved|changed|exam|quiz|"
                         r"urgent|action required|declin|reject|offer|interview", re.I)


def _clip(text, n: int) -> str:
    """One line, no longer than `n`. This block rides along on every message,
    so a single mailing-list subject must not be allowed to spend a paragraph
    of it."""
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1] + "\u2026"


def _max_id(con, sql: str, params=()) -> int:
    row = _rows(con, sql, params)
    return (row[0][0] or 0) if row else 0


def pulse(con) -> dict:
    """Stable revision high-water mark, not source row IDs or elapsed age."""
    today = dt.datetime.now(config.tz()).date().isoformat()
    loc = _rows(con, "SELECT title FROM facts WHERE kind='current' "
                     "ORDER BY ts DESC LIMIT 1")
    return {
        "day": today,
        "place": loc[0]["title"] if loc else None,
        "change_seq": changes.sequence(con),
        "action_id": _max_id(con, "SELECT MAX(id) FROM actions"),
        "health": changes.health(con),
    }


def delta(con, prev: dict | None) -> tuple[str | None, dict]:
    """Bounded notices of net changes; complete evidence remains queryable."""
    now = pulse(con)
    if not prev:
        return None, now
    lines: list[str] = []
    if prev.get("day") and now["day"] != prev["day"]:
        lines.append(f"- The date rolled over. It is now {now['day']}.")
    if now["place"] and now["place"] != prev.get("place"):
        lines.append(f"- Location changed: now {now['place']} "
                     f"(was {prev.get('place') or 'unknown'}).")

    if "change_seq" not in prev:
        # One-time upgrade of an existing topic. Old row IDs cannot reconstruct
        # an interval of updates, so do not manufacture a revision cursor.
        lines.append("- Revision tracking is now active. Earlier edits may not "
                     "have appeared in update notices; query current records "
                     "before relying on older context.")
        revisions = []
    else:
        revisions = changes.net(con, prev["change_seq"], now["change_seq"])

    if "change_seq" in prev and prev["change_seq"] < changes.pruned_through(con):
        lines.append("- Change history for this conversation is incomplete after "
                     "expiry or earlier cleanup; query current records before "
                     "relying on old context.")

    records, mail, quiet_mail, documents, added, senders, other = [], [], 0, [], Counter(), Counter(), Counter()
    for r in revisions:
        before, after = r["before"], r["after"]
        value = after or before
        op = "removed" if after is None else "added" if before is None else "updated"
        entity = r["entity"]
        if entity == "document":
            documents.append(value["path"] + (" (removed)" if after is None else ""))
        elif entity in ("commitments", "deadlines"):
            label = "Commitment" if entity == "commitments" else "Deadline"
            title = _clip(value.get("text") or value.get("title"), 75)
            due = value.get("due") or "no date"
            status = value.get("status") or "unknown"
            ending = " (closed or dropped)" if entity == "commitments" and status != "open" else ""
            records.append(f"- {label} {op}: {title}; due {due}; status {status}{ending}.")
        elif entity in ("opportunities", "applications"):
            label = entity[:-1].capitalize() if entity == "applications" else "Opportunity"
            if before and after and before.get("status") == after.get("status"):
                other[entity] += 1
            elif before is None and entity == "opportunities":
                added[entity] += 1
            else:
                name = " at ".join(x for x in (value.get("title"), value.get("org") or value.get("company")) if x)
                records.append(f"- {label} {op}: {_clip(name, 75)}; status {value.get('status')}.")
        elif entity == "facts":
            data = value.get("data") or {}
            if not isinstance(data, dict):
                data = {}
            kind, source = value.get("kind"), value.get("source")
            if kind == "event" and data.get("role") == "mine":
                title = _clip(value.get("title"), 65)
                when = value.get("ts") or "no time"
                location = _clip(data.get("location"), 45)
                records.append(f"- Your event {op}: {title}; {when}"
                               + (f"; {location}" if location else "") + ".")
            elif source in ("gmail", "purelymail") and kind == "message":
                if data.get("bulk") and not MAIL_SIGNAL.search(value.get("title") or ""):
                    quiet_mail += 1
                    continue
                sender = _clip((data.get("from") or "?").split("<")[0].strip().strip('"'), 32)
                mail.append(f"- Email {op}: {sender}: {_clip(value.get('title'), 68)}.")
            elif kind == "message" and data.get("direction") == "incoming" and op == "added":
                senders[_clip(value.get("title"), 24)] += 1
            else:
                other[source] += 1

    # Records first, and never cut by mail: a moved deadline matters more than
    # any newsletter, and an unranked list once hid exactly that behind one.
    lines.extend(records[:DELTA_RECORD_LIMIT])
    if len(records) > DELTA_RECORD_LIMIT:
        lines.append(f"- {len(records) - DELTA_RECORD_LIMIT} more record changes; "
                     "use the revision range below.")
    lines.extend(mail[:DELTA_ITEM_LIMIT])
    if len(mail) > DELTA_ITEM_LIMIT:
        lines.append(f"- {len(mail) - DELTA_ITEM_LIMIT} more emails; "
                     "use the revision range below.")
    if quiet_mail:
        lines.append(f"- {quiet_mail} bulk email(s) with no sign of a change or deadline.")
    if senders:
        lines.append("- New messages from: " + ", ".join(
            f"{who} ({n})" for who, n in senders.most_common(5))
            + (f", and {len(senders) - 5} others" if len(senders) > 5 else "") + ".")
    if documents:
        lines.append("- Memory documents changed: " + ", ".join(documents[:4])
                     + (f", and {len(documents) - 4} more" if len(documents) > 4 else "") + ".")
    for entity, n in added.items():
        lines.append(f"- {n} new {entity} recorded.")
    if other:
        lines.append(f"- {sum(other.values())} other record(s) changed: " + ", ".join(
            f"{name} {n}" for name, n in other.most_common(4)) + ".")
    if revisions:
        lines.append(f"- Full change evidence: query ledger_changes with "
                     f"seq > {prev['change_seq']} AND seq <= {now['change_seq']}.")

    # Routine writes by one collector are one fact, however many rows they took.
    acts = _rows(con, "SELECT kind,target,summary FROM actions WHERE id>? ORDER BY id LIMIT 500",
                 (prev.get("action_id", 0),))
    groups: dict[tuple, list] = {}
    for a in acts:
        groups.setdefault((a["kind"], a["target"]), []).append(a)
    for (kind, target), rows in list(groups.items())[:DELTA_ITEM_LIMIT]:
        if len(rows) > 2:
            lines.append(f"- Herald wrote {len(rows)} times: {kind} on {_clip(target, 28)}.")
        else:
            lines.extend(f"- Herald write: {kind} on {_clip(target, 28)}: "
                         f"{_clip(a['summary'], 80)}" for a in rows)
    if len(groups) > DELTA_ITEM_LIMIT:
        lines.append(f"- More writes are available in actions after id {prev.get('action_id', 0)}.")

    if now["health"] != prev.get("health", {}):
        lines.extend(changes.health_lines(now["health"]))
        for name in prev.get("health", {}):
            if name not in now["health"]:
                lines.append(f"- {name}: source coverage recovered; the latest sync is complete.")
    if not lines:
        return None, now
    return ("## Since your last message\n"
            "Generated deterministically, not by anyone. Nothing here is a\n"
            "request: it is what moved in the ledger while you were talking.\n\n"
            + "\n".join(lines)), now


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "dawn"
    with db.session() as c:
        if which == "orientation":
            print(orientation(c))
        else:
            text, refs = build(c, which)
            print(text)
            print(f"\n[{len(refs)} referenceable items]", file=sys.stderr)
