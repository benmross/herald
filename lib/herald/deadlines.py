"""Dated things live on the calendar, not on a to-do list.

Until 23 September 2026 every quiz, exam and due date Herald found went into
the `commitments` table, beside things like "reply to your dad". The user
reviewed that table for the first time and found 77 rows he had never been
shown, about 45 of them fixed syllabus dates. His verdict was that a date
belongs where he already looks for dates: on his class calendar, with the class
meeting itself saying QUIZ or TEST in a different colour, and every due date
beside it. The obligation list is for open loops (`obligations.py`).

So this module keeps one calendar equal to the `deadlines` table, the same way
`calsync.py` keeps a calendar equal to a scrape:

- **Feeds fill the table.** Every row of a configured feed (a Canvas `.ics`,
  read by `collectors/calendar_feeds.py`) becomes a row here, keyed by the
  feed's own id, so a moved due date moves the row rather than adding one.
  Anything a feed does not publish (syllabus-only quiz dates) is added with
  `herald deadline add`, source `manual`.
- **A quiz or test on a day the course meets marks that meeting.** The class
  event is retitled `QUIZ: <its title>` or `TEST: <its title>` and recoloured.
  What it said before is stamped into its private properties, so when the
  test moves or is cancelled the meeting goes back to exactly what it was.
- **Everything else is its own event**, keyed `deadline:<id>` and synced by
  `calsync`: a due date is a short block ending at the moment it is due (or an
  all-day event when the feed gives only a day), and a test with no class
  meeting that day (a final) is an event at its own time.

Nothing here decides anything. Which rows exist is the feeds' and the user's
call; this only makes the calendar agree, and every write goes through gwrite
so it lands in `actions` and the digest.
"""

from __future__ import annotations

import datetime as dt
import re

from . import calsync, config, db, gwrite

ACTOR = "collector:deadlines"
KEY_PREFIX = "deadline:"
KINDS = ("quiz", "test", "due", "event")

# A pass that wants to restore more class meetings than this is reading a
# broken table, not a real change. Same reasoning as calsync's caps.
MAX_RESTORES_PER_PASS = 20

_QUIZ = re.compile(r"\bquiz", re.I)
_TEST = re.compile(r"\b(exam|midterm|test|final)\b", re.I)


def _cfg() -> dict:
    return config.get("deadlines", {}) or {}


def classify(title: str) -> str:
    """quiz, test or due, from a feed's title. Feeds do not say."""
    if _QUIZ.search(title or ""):
        return "quiz"
    if _TEST.search(title or ""):
        return "test"
    return "due"


def _norm(code: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def add(con, *, source: str, ref: str | None, course: str | None, kind: str,
        title: str, due: str, end_at: str | None = None,
        notes: str | None = None) -> int:
    """Insert or refresh one row; returns its id. (source, ref) is identity,
    so a feed re-reading the same item updates it in place."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    now = db.now()
    if ref is not None:
        row = con.execute("SELECT id FROM deadlines WHERE source=? AND ref=?",
                          (source, ref)).fetchone()
        if row:
            con.execute("UPDATE deadlines SET course=?, kind=?, title=?, due=?,"
                        " end_at=?, notes=coalesce(?, notes), updated_at=?"
                        " WHERE id=?",
                        (course, kind, title, due, end_at, notes, now, row[0]))
            return row[0]
    cur = con.execute(
        "INSERT INTO deadlines (source, ref, course, kind, title, due, end_at,"
        " notes, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,'live',?,?)",
        (source, ref, course, kind, title, due, end_at, notes, now, now))
    return cur.lastrowid


def import_feeds(con) -> int:
    """Copy configured feed facts into the table. Returns rows touched."""
    n = 0
    since = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    for feed in _cfg().get("feeds") or []:
        skip = re.compile(feed["skip_titles"], re.I) if feed.get("skip_titles") else None
        rows = con.execute(
            "SELECT external_id, ts, title, json_extract(data, ?) course"
            " FROM facts WHERE source=? AND kind=? AND ts >= ?",
            (f"$.{feed.get('course_field', 'tag')}", feed["source"],
             feed.get("kind", "assignment"), since)).fetchall()
        for ext, ts, title, course in rows:
            if skip and skip.search(title or ""):
                continue
            kind = classify(title)
            label = title if not course or _norm(course) in _norm(title) else f"{course} {title}"
            add(con, source=feed["source"], ref=ext, course=course, kind=kind,
                title=label, due=ts)
            n += 1
    return n


# --------------------------------------------------------------------------
# Matching a quiz or test to the class meeting it happens in
# --------------------------------------------------------------------------

def _start(ev: dict) -> str:
    return (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date") or ""


def _private(ev: dict) -> dict:
    return ((ev.get("extendedProperties") or {}).get("private")) or {}


def _orig_summary(ev: dict) -> str:
    p = _private(ev)
    return p.get("heraldOrigSummary", "") if p.get("heraldTag") else ev.get("summary", "")


def meeting_for(row: dict, meetings: list[dict]) -> dict | None:
    """The class meeting a quiz or test happens in: same course, same day,
    and the one nearest the row's time when it has one."""
    code = _norm(row["course"])
    if not code:
        return None
    day = row["due"][:10]
    same = [m for m in meetings
            if _norm(_orig_summary(m)).startswith(code) and _start(m)[:10] == day
            and "dateTime" in (m.get("start") or {})]
    if not same:
        return None
    if len(row["due"]) <= 10:
        return min(same, key=_start)
    t = dt.datetime.fromisoformat(row["due"])
    if t.tzinfo is None:
        t = t.replace(tzinfo=config.tz())
    return min(same, key=lambda m: abs(dt.datetime.fromisoformat(_start(m)) - t))


def _body(row: dict) -> dict:
    """The event for a row that is not riding on a class meeting."""
    cfg = _cfg()
    prefix = (cfg.get("prefix") or {}).get(row["kind"], "")
    color = (cfg.get("colors") or {}).get(row["kind"], "")
    summary = f"{prefix}: {row['title']}" if prefix else row["title"]
    due, end = row["due"], row["end_at"]
    all_day = len(due) <= 10
    if not all_day:
        start_t = dt.datetime.fromisoformat(due)
        if start_t.tzinfo is None:
            start_t = start_t.replace(tzinfo=config.tz())
        if row["kind"] == "due":
            # A block that ends at the deadline, so the bottom edge is the moment.
            mins = int(cfg.get("due_block_minutes", 30))
            end = start_t.isoformat()
            start_t = start_t - dt.timedelta(minutes=mins)
        elif not end:
            mins = int(cfg.get("test_minutes", 120) if row["kind"] in ("quiz", "test") else 60)
            end = (start_t + dt.timedelta(minutes=mins)).isoformat()
        due = start_t.isoformat()
    desc = "\n".join(x for x in (row["notes"] or "",
                                 f"Tracked by Herald (deadline #{row['id']}, from {row['source']}).")
                     if x)
    return gwrite.build_event(summary=summary, start=due, end=end, all_day=all_day,
                              description=desc, color_id=color,
                              sync_key=f"{KEY_PREFIX}{row['id']}")


def _raw_window(cal_id: str, lo: dt.datetime, hi: dt.datetime) -> list[dict]:
    """Every event on the class calendar, whole, since the tag pass needs the
    private properties `gwrite.summarize` drops. Read only."""
    svc = gwrite.calendar_service()
    items, tok = [], None
    while True:
        r = svc.events().list(calendarId=cal_id, timeMin=lo.isoformat(),
                              timeMax=hi.isoformat(), singleEvents=True,
                              orderBy="startTime", showDeleted=False,
                              maxResults=250, pageToken=tok).execute()
        items += r.get("items", [])
        tok = r.get("nextPageToken")
        if not tok:
            return items


def plan_tags(meetings: list[dict], tags: dict[str, list[dict]]) -> list[tuple]:
    """(op, event, patch, what) for every class meeting whose QUIZ/TEST mark
    is wrong. Pure; `sync` applies it."""
    cfg = _cfg()
    prefixes = cfg.get("prefix") or {}
    colors = cfg.get("colors") or {}
    ops = []
    for ev in meetings:
        p = _private(ev)
        rows = tags.get(ev["id"], [])
        tagged = bool(p.get("heraldTag"))
        orig = _orig_summary(ev)
        if rows:
            kind = "test" if any(r["kind"] == "test" for r in rows) else "quiz"
            key = ",".join(str(r["id"]) for r in sorted(rows, key=lambda r: r["id"]))
            want = f"{prefixes.get(kind, kind.upper())}: {orig}"
            color = colors.get(kind, "")
            titles = "; ".join(r["title"] for r in rows)
            if tagged and not ev.get("summary", "").startswith(
                    tuple(f"{v}: " for v in prefixes.values() if v)):
                ops.append(("skip", ev, None, f"'{ev.get('summary')}' was renamed by hand"))
                continue
            if (ev.get("summary") == want and (ev.get("colorId") or "") == color
                    and p.get("heraldTag") == key):
                continue
            patch = {"summary": want, "colorId": color or None,
                     "extendedProperties": {"private": {
                         "heraldTag": key,
                         "heraldTitles": titles[:1000],
                         "heraldOrigSummary": orig,
                         "heraldOrigColor": (p.get("heraldOrigColor", "") if tagged
                                             else ev.get("colorId", "") or ""),
                     }}}
            ops.append(("tag", ev, patch, f"marked '{orig}' as {want.split(':')[0]} ({titles})"))
        elif tagged:
            patch = {"summary": orig, "colorId": p.get("heraldOrigColor") or None,
                     "extendedProperties": {"private": {
                         "heraldTag": "", "heraldTitles": "",
                         "heraldOrigSummary": "", "heraldOrigColor": ""}}}
            ops.append(("restore", ev, patch, f"restored '{orig}', no longer a quiz or test day"))
    return ops


def sync(con, *, dry_run: bool = False, report=print) -> dict:
    cfg = _cfg()
    cal_name = cfg.get("calendar")
    if not cal_name:
        return {"skipped": "deadlines.calendar is not set"}
    cal_id = gwrite.calendar_id(cal_name)
    if not cal_id:
        raise RuntimeError(f"no calendar named {cal_name!r}")
    class_name = cfg.get("class_calendar") or cal_name
    class_id = cal_id if class_name == cal_name else gwrite.calendar_id(class_name)

    imported = import_feeds(con)
    today = dt.date.today()
    tz = config.tz()
    lo = dt.datetime.combine(today, dt.time(), tz)
    hi = lo + dt.timedelta(days=int(cfg.get("window_days", 120)))
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM deadlines WHERE substr(due,1,10) >= ? AND substr(due,1,10) < ?",
        (lo.date().isoformat(), hi.date().isoformat()))]
    live_rows = [r for r in rows if r["status"] == "live"]

    raw = _raw_window(class_id, lo, hi) if class_id else []
    meetings = [e for e in raw
                if not _private(e).get("syncKey", "").startswith(KEY_PREFIX)
                and not (e.get("summary") or "").startswith(gwrite.CANCEL_PREFIX)]
    tags: dict[str, list[dict]] = {}
    standalone = []
    for r in live_rows:
        m = meetings and r["kind"] in ("quiz", "test") and meeting_for(r, meetings)
        if m:
            tags.setdefault(m["id"], []).append(r)
        else:
            standalone.append(r)

    tag_ops = plan_tags(meetings, tags)
    restores = [o for o in tag_ops if o[0] == "restore"]
    if len(restores) > MAX_RESTORES_PER_PASS:
        report(f"refusing to restore {len(restores)} class meetings in one pass "
               f"(limit {MAX_RESTORES_PER_PASS}); the table is probably wrong")
        tag_ops = [o for o in tag_ops if o[0] != "restore"]

    want = {f"{KEY_PREFIX}{r['id']}": _body(r) for r in standalone}
    cancelled = {f"{KEY_PREFIX}{r['id']}" for r in rows if r["status"] == "cancelled"}
    # A row now riding on a class meeting does not want its own event any more;
    # that event was never cancelled, so it is removed rather than greyed.
    ignored = {f"{KEY_PREFIX}{r['id']}": "now marked on the class meeting"
               for rs in tags.values() for r in rs}
    live = gwrite.calendar_window(cal_id, lo, hi)
    ops = calsync.plan(live, want, cancelled, {KEY_PREFIX.rstrip(":")}, today, ignored)
    ops = calsync.guard(ops, report=report)

    if dry_run:
        for op, ev, _patch, what in tag_ops:
            report(f"{op:9} {_start(ev)[:16]:17} {what}")
        for line in calsync.describe(ops):
            report(line)
    else:
        for op, ev, patch, what in tag_ops:
            if op == "skip":
                report(what)
                continue
            gwrite.calendar_patch(con, actor=ACTOR, cal_id=class_id, cal_name=class_name,
                                  event_id=ev["id"], patch=patch, what=what)
    counts = calsync.apply(con, ops, actor=ACTOR, cal_id=cal_id, cal_name=cal_name,
                           purgeable_prefixes=(KEY_PREFIX,), dry_run=dry_run,
                           report=report)
    counts = {k: v for k, v in counts.items() if v}
    counts["feed rows"] = imported
    for op in ("tag", "restore", "skip"):
        n = sum(1 for o in tag_ops if o[0] == op)
        if n:
            counts[{"tag": "meetings marked", "restore": "meetings restored",
                    "skip": "left alone (hand-edited)"}[op]] = n
    return counts


def upcoming(con, days: int = 14) -> list[dict]:
    today = dt.date.today()
    return [dict(r) for r in con.execute(
        "SELECT * FROM deadlines WHERE status='live' AND substr(due,1,10) >= ?"
        " AND substr(due,1,10) <= ? ORDER BY replace(due, ' ', 'T')",
        (today.isoformat(), (today + dt.timedelta(days=days)).isoformat()))]
