#!/usr/bin/env python
"""Build the brief a cycle reasons over.

This is the deterministic half of a cycle and it is where the token budget is
actually decided. The agent gets one compact document instead of a database and
an invitation to go fishing: every row here was chosen by a query, so a cycle
costs roughly the same whether the ledger holds two thousand facts or two
hundred thousand.

Everything here is a plain SQL read. No judgment, no model.

**It queries by shape, not by source.** The first version named its sources --
one service for deadlines, another for messages, another for location -- which
meant the program knew the name of every service one particular person happened
to use. Now a deadline is
any fact of kind `assignment`, a conversation is any `thread`, a position is any
`current`, wherever it came from. A collector that writes the right shape shows
up in the brief without the program having heard of it.

What genuinely cannot be generalised is contributed by whoever owns it: an
extension may ship a `snapshot.py` with a `section(con, ctx)` function, and its
output lands in the brief in `ORDER` position. Grades and announcements from a
university's Canvas belong there, not here.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import pathlib
import sys
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import config, db, extensions  # noqa: E402

# Near-term detail, then everything else in brief. Nothing is hidden merely for
# being distant: an exam eight weeks out still belongs in the picture, it just
# does not need a line of its own every morning.
DUE_HORIZON_DAYS = 21
SCHEDULE_DAYS = 2
AWAITING_LIMIT = 20
NEW_MAIL_LIMIT = 25

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

    out.append(f"# Snapshot — {cycle} cycle — {now:%A %-d %B %Y, %H:%M %Z}")
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
        age = _j(r, "age_minutes")
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
                out.append(f"    - {a['ts'][11:16]} {a['summary']}")
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
        evs = [e for e in evs if _j(e, "calendar") not in ignored]

        # Separated by provenance, because mixing them is how a machine-filled
        # firehose calendar gets read as the user's schedule -- or worse, as
        # evidence that they belong to something. `role` comes from
        # calendars.roles in the config and is the whole point of that setting.
        buckets = {"mine": [], "other-person": [], "reference": [], "feed": []}
        for e in evs:
            buckets.setdefault(_j(e, "role") or "feed", buckets["feed"]).append(e)

        def fmt(e):
            when = e["ts"][11:16] if len(e["ts"] or "") > 11 else "all day"
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
        clock = a["ts"][11:16] if len(a["ts"] or "") > 11 else ""
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
    head("New mail since the last cycle")
    if since:
        fresh = _rows(con, """
            SELECT * FROM facts
            WHERE source='gmail' AND kind='message' AND ts > ?
              AND json_extract(data,'$.from_me') = 0
            ORDER BY ts DESC LIMIT ?
        """, (since, NEW_MAIL_LIMIT))
    else:
        fresh = _rows(con, """
            SELECT * FROM facts
            WHERE source='gmail' AND kind='message'
              AND json_extract(data,'$.from_me') = 0
            ORDER BY ts DESC LIMIT ?
        """, (NEW_MAIL_LIMIT,))
    if not fresh:
        out.append("nothing new")
    for m in fresh:
        mark = "bulk" if _j(m, "bulk") else "direct"
        out.append(f"- [{mark}] {(m['ts'] or '')[:16].replace('T', ' ')}  "
                   f"{_j(m, 'from')}  —  {m['title']}")
        if snippet := (m["body"] or "").strip():
            out.append(f"      {snippet[:180]}")

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


if __name__ == "__main__":
    with db.session() as c:
        text, refs = build(c, sys.argv[1] if len(sys.argv) > 1 else "dawn")
    print(text)
    print(f"\n[{len(refs)} referenceable items]", file=sys.stderr)
