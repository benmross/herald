#!/usr/bin/env python
"""The scout cycle — decide which candidates are actually opportunities.

The collector already did the cheap half: a few hundred messages down to a few
dozen candidates, by string matching, for free. This is the half that needs to
know who the user is, so it happens once, in one session, against
`identity/goals.md`.

The thing this exists to prevent has a name here: finding out that a company's
recruiting had opened by happening to meet a recruiter at a tailgate. So the
output is not a list — it is rows in the `opportunities` table with deadlines
and states, because something surfaced once and then forgotten is the same as
never having found it.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))

from herald import config, db, notify, think  # noqa: E402

CYCLE = "scout"
LABEL = f"cycle:{CYCLE}"

WHO = config.person()
NAME = WHO["first"]
THEY, THEM, THEIR = WHO["subject"], WHO["object"], WHO["possessive"]
HAVE = "have" if WHO["plural_verb"] else "has"
DO = "do" if WHO["plural_verb"] else "does"
S = "" if WHO["plural_verb"] else "s"

# The first pass cost $2.79 on Opus, which is the right price for reading thirty
# unseen candidates and searching the web for their deadlines, and the wrong
# price for re-reading the same twenty-eight of them twelve hours later. So the
# model is chosen by how much is actually new: a pass with real new material
# gets Opus, a quiet one gets Sonnet.
#
# "New material" stopped meaning "new candidates" on 9 September 2026, when the
# mail filter came out and every message became a candidate. Twenty Target
# receipts are not a reason to spend Opus. So the count is new postings plus
# only the mail the keyword score still rates -- the score's last job, now that
# it no longer decides what gets read.
ESCALATE_AT = 6
ESCALATE_MAIL_SCORE = 2

# A scout pass reads what is new. On the day the internship feed is first
# connected that is 263 postings, which is a fine amount of information and a
# terrible amount to hand one session. Cap it, rank what goes first, and let the
# rest arrive over the following passes -- nothing is lost, because a posting
# stays in the feed until it closes.
MAX_NEW_POSTINGS = 35

# Campus mail handed to the session per pass for calendar extraction. The
# labels are busy; anything beyond this waits for the next pass.
MAX_CAMPUS_MAIL = 15

# Since 9 September 2026 scout reads every message rather than a keyword-
# filtered subset -- a filter cannot know which words will matter, and a
# message it scored zero was never seen by anything that could tell. Every
# message is now a candidate, so
# this is the one thing standing between a quiet cycle and a pass handed four
# hundred messages at once. Same contract as MAX_NEW_POSTINGS: the highest
# scoring go first, the rest are not marked seen and come round next pass.
# About thirty messages arrive between passes, so this only bites while the
# backlog from the change itself drains.
MAX_NEW_MAIL = 120

SCHEMA = {
    "type": "object",
    "properties": {
        "push_now": {
            "type": "array",
            "description": "Opportunities worth interrupting for right now. "
                           "Anything closing within a week belongs here. Empty "
                           "is a perfectly good answer. Every string in here "
                           "reaches the user directly, so write to them, not "
                           "about them.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "why": {"type": "string",
                            "description": "One sentence saying why this one "
                                           "matters, WRITTEN TO THE USER IN THE "
                                           "SECOND PERSON. This goes straight to "
                                           "their phone, so it reads 'you are "
                                           "eligible', never 'they are "
                                           "eligible'."},
                    "deadline": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": ["title", "why"],
            },
        },
        "calendar_events": {
            "type": "array",
            "description": "Concrete campus events found in the campus mail "
                           "section of the snapshot that are not already on the "
                           "calendar: a real date and time, a real place. Python "
                           "writes these to the calendar named by calendars.write.event. Do not "
                           "include anything the scraped feeds already carry, "
                           "recurring meetings without a specific date, or "
                           "anything whose time you had to guess.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start": {"type": "string",
                              "description": "ISO 8601 with offset, e.g. 2026-09-24T18:00:00-04:00, or a bare date for all-day"},
                    "end": {"type": "string"},
                    "all_day": {"type": "boolean"},
                    "location": {"type": "string"},
                    "description": {"type": "string",
                                    "description": "One or two lines. Who is hosting, what it is."},
                    "url": {"type": "string"},
                    "source_message_id": {"type": "string",
                                          "description": "The gmail message id it came from."},
                },
                "required": ["title", "start"],
            },
        },
        "summary": {
            "type": "string",
            "description": "A few lines on what this pass found, for the journal.",
        },
        "opened": {"type": "integer"},
        "rejected": {"type": "integer"},
    },
    "required": ["summary", "opened", "rejected"],
}

INSTRUCTIONS = f"""\
You are running the scout cycle. Nobody is at a keyboard.

Read `ledger/identity/goals.md` first, and `about.md` for context. Everything
below is ranked against those, not against what a generic person in {THEIR}
position would want. The rules that decide what counts as a real opportunity
for *this* person live in those files; this prompt only tells you how to work.

You are **updating** a table, not rebuilding one. The snapshot gives you the
judgments you have already made and only the candidates you have never seen.
Do not re-derive a standing conclusion; new input is here to change it or leave
it alone. If nothing new arrived, check for passed deadlines and stop — a quiet
pass should be a cheap one.

**You are given every email that arrived since the last pass, not a
selection.** This replaced a keyword filter that had hidden things that
mattered: on 9 September 2026 a receipt saying an App Store submission had gone
into review scored zero, and so did a direct question from a family member.
Each message carries a `filter score` and it is a reading order, nothing more.

Most of it is genuinely nothing and should cost you a line each. But read all
of it, and do not read it only for opportunities: if something in there is a
person waiting on a reply, a bill, a deadline buried in a listserv digest, or
a status change on something {THEY} already applied to, say so in `summary` so
it reaches the digest. You have the `herald-ledger` and `google-workspace`
skills; open the full message when the snippet is not enough, and use web
search when a posting has a deadline you cannot see.

For each candidate, decide: is this a real opportunity for *this* person?

Apply the ranking rules in `goals.md`, and these, which hold for anyone:

- **Eligibility is real.** A posting {THEY} cannot apply to is noise, and
  offering it wastes the one thing that makes these worth reading at all. When
  the feed does not say who is eligible, that is the one thing worth opening
  the page to find out.
- **Would {THEY} have found it {WHO["reflexive"]}?** If obviously yes, do not
  surface it. The value here is entirely in what {THEY} would otherwise have
  missed.
- **Rank against the goals, but do not decide for {THEM}.** Something that
  ranks below an alternative still gets surfaced, one line, marked as such.
  Silently dropping a whole category is how an agent starts quietly editing
  someone's options.

**Triage from the line before you open anything.** Each posting arrives as
company, title, location, category and match reason. Most can be settled from
that alone. Only open a posting when the line looks plausible and the one thing
you cannot see is the thing that decides it. Opening thirty-five pages a pass is
how this cycle gets expensive without getting better.

**Watched employers.** The config lists organisations under
`opportunities.watch_employers` that the crowdsourced feeds miss or cover
unevenly — the reason the setting exists is a company with sixteen thousand
postings on an aggregator and not one of its own. Once a day at most — on the
first pass of the day, not the second — search the web for whether any of them
have opened a round, and open a row when one has. Do not repeat a search you
already did today; the journal says what you checked.

For each one that survives, insert a row:

    herald db "insert into opportunities
      (found_at, title, org, kind, deadline, url, fit, score, status)
      values (datetime('now'), '...', '...', 'internship', '2026-10-15',
              'https://...', 'why this one, for this person', 4, 'new')"

`kind` is one of internship, research, scholarship, hackathon, event, job, other.
`score` is 1 to 5. `deadline` is ISO or null. **Check for an existing row on the
same thing before inserting** — the table has a uniqueness constraint on
(title, org) and a duplicate helps nobody.

Also revisit rows already in the table. If something has been overtaken, mark it
`passed` or `expired` with a note.

Then rewrite `ledger/state/opportunities.md` as the readable view: what is live,
sorted by deadline, each with its next action. Append a short entry to today's
journal at `ledger/journal/YYYY-MM-DD.md`.

Finally, return the schema. `push_now` is for things worth interrupting for
immediately rather than waiting for the morning: a real deadline closing soon is
pushed the moment it is found. Good-but-not-urgent finds are worth pushing too
rather than holding — err towards telling {THEM}, and {THEY} will say if it
becomes annoying.

**Write `push_now` to {THEM}, not about {THEM}.** Everything in it lands on a
screen {THEY} are looking at: "You have three days", never "{NAME} has three
days". The rest of this prompt discusses {THEM} in the third person and it is
very easy to carry that register into the output. Re-read each `why` before you
return it.

**Mail → calendar.** The snapshot may end with a section of mail from labels
{THEY} chose (`scout.event_mail_labels` in the config): listservs, club mail,
announcements. For each concrete event in that mail — a named thing at a real
date, time and place — that the ledger does not already carry, return it in
`calendar_events`. Python writes them to the calendar named by
`calendars.write.event`, which is a firehose rather than a commitment list, so
breadth is fine and duplicates are not: if the same event is plainly in the
"already on the calendar" list, skip it. Never guess a time; an event with no
time is not a calendar event. Open the full message when the snippet is not
enough.

You may not apply to anything, register for anything, RSVP, or send any message.
"""

def _brain_url() -> str | None:
    try:
        out = subprocess.run([str(config.ROOT / "bin" / "herald-brain"), "url"],
                             capture_output=True, text=True, timeout=20)
        return out.stdout.strip() or None
    except Exception:                                     # noqa: BLE001
        return None


def _seen(con) -> set[str]:
    row = con.execute(
        "SELECT cursor FROM collector_state WHERE collector = ?", (CYCLE,)).fetchone()
    if not row or not row["cursor"]:
        return set()
    try:
        return set(json.loads(row["cursor"]))
    except (json.JSONDecodeError, TypeError):
        return set()


def _remember(con, ids: set[str]) -> None:
    # Bounded: a candidate that has fallen out of the mail window will never be
    # offered again, so remembering it forever buys nothing.
    keep = sorted(ids)[-4000:]
    con.execute("""
        INSERT INTO collector_state (collector, last_run, last_ok, cursor)
        VALUES (?, datetime('now'), datetime('now'), ?)
        ON CONFLICT (collector) DO UPDATE SET
            last_run = excluded.last_run, last_ok = excluded.last_ok,
            cursor = excluded.cursor
    """, (CYCLE, json.dumps(keep)))
    con.commit()


def build_snapshot(con, fresh: set[str]) -> tuple[str, set[str]]:
    """Standing conclusions, plus only what has not been judged before.

    The whole candidate list used to go in every pass, which meant the same
    twenty-eight listserv digests were re-read and re-rejected twice a day. What
    the session actually needs is what it already decided and what is new.

    Returns the text and the ids it actually put in front of the session.
    Anything held back by a cap must not be marked seen, or it is silently
    dropped forever; the caller used to reconstruct that set by sorting
    external ids, which is not the order anything was shown in.
    """
    now = dt.datetime.now(config.tz())
    shown_ids: set[str] = set()
    out = [f"# Scout snapshot — {now:%A %-d %B %Y, %H:%M}"]

    live = con.execute("""
        SELECT * FROM opportunities ORDER BY
            CASE status WHEN 'new' THEN 0 WHEN 'surfaced' THEN 1
                        WHEN 'interested' THEN 2 ELSE 3 END,
            COALESCE(deadline, '9999')
    """).fetchall()
    out.append(f"\n## What you already concluded ({len(live)} rows)\n")
    out.append("These are standing judgments. Do not re-derive them. Revisit one "
               "only if something below changes it, or if its deadline has "
               "passed.\n")
    if not live:
        out.append("nothing yet — this is the first scout pass")
    for o in live:
        out.append(f"- #{o['id']} [{o['status']}] due {o['deadline'] or '—'} "
                   f"score {o['score']}  **{o['title']}** ({o['org'] or '?'})")
        if o["fit"]:
            out.append(f"    {o['fit'][:180]}")

    postings = con.execute("""
        SELECT * FROM facts WHERE source='internships' AND kind='posting'
        ORDER BY
          -- What they actually do comes first. Ranking "watched employer" above
          -- everything put Microsoft hardware roles at the top of a list for
          -- someone who wants machine learning and system architecture.
          CASE WHEN json_extract(data,'$.category') IN
                    ('AI/ML/Data','Software','Software Engineering',
                     'Data Science, AI & Machine Learning') THEN 0 ELSE 1 END,
          CASE WHEN json_extract(data,'$.why') LIKE '%early-career%' THEN 0
               WHEN json_extract(data,'$.why') LIKE '%DMV%'          THEN 1
               WHEN json_extract(data,'$.why') LIKE '%watched%'      THEN 2
               ELSE 3 END,
          ts DESC
    """).fetchall()
    new_postings = [r for r in postings if r["external_id"] in fresh]
    shown = new_postings[:MAX_NEW_POSTINGS]
    shown_ids |= {r["external_id"] for r in shown}

    if new_postings:
        out.append(f"\n## New internship postings ({len(new_postings)} new, "
                   f"showing {len(shown)})\n")
        out.append("From the crowdsourced Summer 2027 lists, already filtered to "
                   "DMV, remote, watched employers, or a named early-career "
                   "programme. **Nothing in this feed states year eligibility** — "
                   "3,082 active listings and not one names a first- or "
                   "second-year track in its title, so if a posting looks right, "
                   "open it and check whether a freshman can apply before "
                   "surfacing it.\n")
        for r in shown:
            d = json.loads(r["data"] or "{}")
            out.append(f"- **{r['title']}** — {r['body'] or 'location n/a'}")
            out.append(f"    {d.get('category')} · matched on "
                       f"{', '.join(d.get('why') or [])} · {d.get('url')}")
        if len(new_postings) > len(shown):
            out.append(f"\n({len(new_postings) - len(shown)} more will come "
                       f"through on the next pass.)")

    rows = con.execute("""
        SELECT * FROM facts WHERE source='opportunities' AND kind='candidate'
        ORDER BY json_extract(data,'$.score') DESC, ts DESC
    """).fetchall()
    rows = [r for r in rows if r["external_id"] in fresh]
    held_back = max(0, len(rows) - MAX_NEW_MAIL)
    rows = rows[:MAX_NEW_MAIL]
    shown_ids |= {r["external_id"] for r in rows}

    out.append(f"\n## New in {THEIR} mail ({len(rows)})\n")
    if not rows:
        out.append("Nothing new arrived. Check whether any deadline above has "
                   "passed, then stop — a quiet pass should be a cheap one.\n")
    else:
        out.append(
            f"**This is every message that reached {THEM} since the last pass, "
            "not a filtered selection.** A keyword filter cannot know in "
            "advance which words "
            "matter and a message it scored zero was never seen by anything "
            "that could tell. Most of what follows is genuinely nothing — "
            "receipts, shipping, security alerts — and skimming past those "
            "costs a line each. The `filter score` is a reading order, not a "
            "verdict: high first, `junk` last. Read all of it anyway, and "
            "notice the things that are not opportunities but still matter: "
            "a person waiting on a reply, a bill, a deadline buried in a "
            "listserv digest, an application that has moved.\n")
        if held_back:
            out.append(f"({held_back} more are queued and will come through on "
                       f"the next pass.)\n")
    for r in rows:
        d = json.loads(r["data"] or "{}")
        out.append(f"\n### {r['title']}")
        out.append(f"- from: {d.get('from')}")
        out.append(f"- when: {(r['ts'] or '')[:16].replace('T', ' ')}")
        out.append(f"- filter score {d.get('score')} ({', '.join(d.get('reasons', []))})")
        if r["body"]:
            out.append(f"- snippet: {r['body'][:400]}")

    # --- campus mail, for the calendar ----------------------------------
    # The labels the user applies themselves (a Gmail filter does most of it). Message
    # rows carry label ids; the gmail collector keeps the id -> name map.
    labels = list(config.get("scout.event_mail_labels", []) or [])
    label_ids = []
    if labels:
        marks = ",".join("?" for _ in labels)
        label_ids = [r[0] for r in con.execute(
            "SELECT external_id FROM facts WHERE source='gmail' AND kind='label'"
            f" AND title IN ({marks})", labels)]
    since = con.execute("SELECT MAX(ts) FROM runs WHERE label = ?", (LABEL,)).fetchone()[0]
    mail = []
    if label_ids:
        rows = con.execute("""
            SELECT * FROM facts WHERE source='gmail' AND kind='message'
              AND ts > COALESCE(?, datetime('now', '-3 days'))
            ORDER BY ts DESC LIMIT 60
        """, (since,)).fetchall()
        for r in rows:
            d = json.loads(r["data"] or "{}")
            if set(d.get("labels") or []) & set(label_ids):
                mail.append((r, d))
    if mail:
        out.append(f"\n## Event mail since the last pass ({len(mail[:MAX_CAMPUS_MAIL])})\n")
        out.append(f"Labelled {' / '.join(labels)} by their own filters. Pull concrete "
                   "events out of these into `calendar_events` (see the instructions). "
                   "Message ids are for the google-workspace skill if a snippet is "
                   "not enough.\n")
        for r, d in mail[:MAX_CAMPUS_MAIL]:
            out.append(f"- `{r['external_id']}` {(r['ts'] or '')[:10]} **{r['title']}** "
                       f"— from {d.get('from')}")
            if r["body"]:
                out.append(f"    {r['body'][:300]}")
        on_cal = con.execute("""
            SELECT ts, title FROM facts WHERE source='gcal' AND kind='event'
              AND json_extract(data,'$.calendar') = ?
              AND ts >= date('now') ORDER BY ts LIMIT 400
        """, (_event_calendar(),)).fetchall()
        if on_cal:
            out.append(f"\nAlready on that calendar ({len(on_cal)}), so not "
                       "wanted again:")
            out.extend(f"- {e['ts'][:16]} {e['title'][:70]}" for e in on_cal)

    return "\n".join(out), shown_ids


def _event_calendar() -> str:
    """Where events found in mail go.

    `calendars.write.event` is the general key. The old
    `calendars.write.campus_event` is still read, because this instance's
    config used it before there was anything general about it.
    """
    return (config.get("calendars.write.event")
            or config.get("calendars.write.campus_event")
            or config.get("calendars.write.default", "primary"))


def _write_calendar_events(events: list[dict]) -> int:
    """Put email-derived events on the event calendar, skipping any that a
    live event already covers by title and start."""
    from herald import gwrite  # noqa: PLC0415
    cal_name = _event_calendar()
    cal_id = gwrite.calendar_id(cal_name)
    if not cal_id:
        print(f"  no calendar named {cal_name!r}; dropping {len(events)} events",
              file=sys.stderr)
        return 0
    tz = config.tz()
    lo = dt.datetime.now(tz) - dt.timedelta(days=1)
    live = gwrite.calendar_window(cal_id, lo, lo + dt.timedelta(days=120))
    written = 0
    with db.session() as con:
        for ev in events:
            start = (ev.get("start") or "").strip()
            title = (ev.get("title") or "").strip()
            if not start or not title:
                continue
            all_day = bool(ev.get("all_day")) or len(start) == 10
            nt = gwrite.norm_title(title)
            if any(gwrite.norm_title(e["summary"]) == nt
                   and gwrite.same_start(e["start"], start) for e in live):
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
            key = f"email:{slug}-{start[:10]}"
            if any(e["syncKey"] == key for e in live):
                continue
            body = gwrite.build_event(
                summary=title, start=start, end=ev.get("end") or None,
                all_day=all_day, location=ev.get("location") or "",
                description=(ev.get("description") or "").strip()
                            + (f"\n\n{ev['url']}" if ev.get("url") else ""),
                color_id="6", sync_key=key, source_url=ev.get("url") or None,
                source_title="Email")
            try:
                gwrite.calendar_insert(con, actor=LABEL, cal_id=cal_id,
                                       cal_name=cal_name, body=body)
                written += 1
            except Exception as e:  # noqa: BLE001
                print(f"  calendar write failed for {title!r}: {e}", file=sys.stderr)
    return written


def main() -> int:
    now = dt.datetime.now(config.tz())
    with db.session() as con:
        ids = {r[0] for r in con.execute(
            "SELECT external_id FROM facts WHERE source IN"
            " ('opportunities','internships')"
            " AND kind IN ('candidate','posting')")}
        seen = _seen(con)
        fresh = ids - seen
        snapshot, shown_ids = build_snapshot(con, fresh)
        # Volume is no longer a proxy for substance: every message is a
        # candidate now, so this counts postings plus the mail the keyword
        # score still rates rather than everything that arrived.
        low = con.execute(f"""
            SELECT COUNT(*) FROM facts
            WHERE source='opportunities' AND kind='candidate'
              AND json_extract(data,'$.score') < ?
              AND external_id IN ({','.join('?' * len(shown_ids)) or "''"})
        """, (ESCALATE_MAIL_SCORE, *sorted(shown_ids))).fetchone()[0]
        substantive = len(shown_ids) - low

    escalate = substantive >= ESCALATE_AT

    snap_dir = config.RAW / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / f"{now:%Y-%m-%d}-{CYCLE}.md").write_text(snapshot)

    result = think.think(
        f"{INSTRUCTIONS}\n\n---\n\n{snapshot}",
        label=LABEL, cwd=config.ROOT, json_schema=SCHEMA, escalate=escalate,
        permission_mode="auto",
        allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Skill",
                       "WebSearch", "WebFetch",
                       "Bash(herald db *)", "Bash(herald status *)",
                       "Bash(~/.claude/skills/google-workspace/scripts/grun *)"],
    )

    if not result.ok:
        notify.push("Herald: scout cycle failed", (result.error or "unknown")[:400],
                    priority="high", tags="rotating_light")
        print(result.error, file=sys.stderr)
        return 1

    payload = result.json() or {}
    pushes = payload.get("push_now") or []

    if pushes:
        lines = []
        for opp in pushes:
            lines.append(f"*{opp['title']}*")
            if opp.get("deadline"):
                lines.append(f"_due {opp['deadline']}_")
            lines.append(opp.get("why", ""))
            if opp.get("url"):
                lines.append(opp["url"])
            lines.append("")
        headline = (pushes[0]["title"] if len(pushes) == 1
                    else f"{len(pushes)} things worth a look")
        notify.tell(f"Found: {headline}"[:120], "\n".join(lines).rstrip(),
                    priority="high", tags="dart")

    # Calendar events the session pulled out of campus mail. Python owns the
    # write, through gwrite, so each one is logged for the digest; the campus
    # calendar sync later adopts any of these that a scraped feed also carries.
    events = payload.get("calendar_events") or []
    written = 0
    if events:
        written = _write_calendar_events(events)

    with db.session() as con:
        # Remember only what the session was actually shown. Anything a cap
        # held back is still unseen and must come round again -- which is why
        # build_snapshot reports its own ids rather than the caller guessing
        # them from a sorted list that was never the display order.
        _remember(con, seen | shown_ids)
        live = con.execute(
            "SELECT COUNT(*) FROM opportunities WHERE status IN"
            " ('new','surfaced','interested')").fetchone()[0]

    cost = f"${result.cost_usd:.4f}" if result.cost_usd is not None else "?"
    if events:
        print(f"  calendar: {written} of {len(events)} email-derived events written")
    print(f"{CYCLE}: {len(fresh)} new ({len(shown_ids)} shown, "
          f"{substantive} substantive), "
          f"opened {payload.get('opened', 0)}, "
          f"rejected {payload.get('rejected', 0)}, "
          f"pushed {len(pushes)}, {live} live  "
          f"[{result.engine}/{result.model} {cost}]")
    print(payload.get("summary", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
