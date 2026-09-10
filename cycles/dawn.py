#!/usr/bin/env python
"""The dawn cycle — the morning digest.

The structure every cycle follows:

    collect (already done, on its own timer)
      -> snapshot   deterministic, Python, free
      -> ONE agent session
      -> apply      deterministic, Python
      -> notify

One session, not one per concern. The snapshot is what keeps that affordable:
the agent gets a page of curated rows rather than a database and an invitation
to go looking, so the cycle costs about the same whether the ledger holds two
thousand facts or two hundred thousand.

The agent writes the ledger itself — updating state and the journal are green
actions and it has the tools. What comes back through the schema is only what
Python needs to deliver: a headline, a body, and a priority. Nothing that leaves
the machine is left to prose.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
sys.path.insert(0, str(HERE))

from _snapshot import build  # noqa: E402
from herald import config, db, notify, think  # noqa: E402

CYCLE = "dawn"
LABEL = f"cycle:{CYCLE}"

# The digest is *about* one person and *addressed to* them, so the prompt needs
# both their name and the pronouns to talk about them in the third person while
# insisting the output is second person. Getting that wrong produces a digest
# that reads like a report on a stranger, which is the single most common way
# a personal agent sounds wrong.
WHO = config.person()
NAME = WHO["first"]
THEY, THEM, THEIR = WHO["subject"], WHO["object"], WHO["possessive"]
HAVE = "have" if WHO["plural_verb"] else "has"

SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "string",
            "description": "One line, under 80 characters. The single most "
                           "important thing about today. This is the phone "
                           "notification title, so it must stand alone -- and "
                           "it is addressed to the user, so second person.",
        },
        "digest": {
            "type": "string",
            "description": "The digest itself, in Markdown. Short. Ordered by "
                           "what actually matters to the user today.",
        },
        "priority": {
            "type": "string",
            "enum": ["min", "low", "default", "high", "urgent"],
            "description": "How hard to buzz their watch. 'default' for an "
                           "ordinary morning. 'high' only when something is "
                           "due today or someone is genuinely waiting.",
        },
        "questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Things you wanted to do but could not without "
                           "their say-so, phrased so they can answer yes or no.",
        },
    },
    "required": ["headline", "digest", "priority"],
}

INSTRUCTIONS = f"""\
You are running the dawn cycle. Nobody is at a keyboard: this is a scheduled,
unattended run, and the only thing {NAME} will see is a notification and the
digest you write.

Below is a snapshot built from the ledger a moment ago. It is already filtered —
you do not need to go re-query everything it contains, though you may query for
anything it does not.

Read `ledger/identity/` and `ledger/state/` first, so the digest reflects what
matters to *{THEM}* rather than what merely happened.

Do these, in this order:

1. **Rewrite `ledger/state/now.md`.** Today's real shape: where {THEY} need{"" if WHO["plural_verb"] else "s"} to be,
   what is due, anything time-critical. Replace the file; it is working memory,
   not a record. Stale state here makes everything downstream a lie.

2. **Reconcile `ledger/state/commitments.md` and the `commitments` table.**
   Open a row for anything {THEY} {HAVE} taken on that nothing is tracking — an
   unanswered email that needs an answer, an accepted deadline, a promise. Close
   anything that has been settled. Check for duplicates before inserting.
   Use `herald db "insert into commitments (created_at, text, kind, due, status)
   values (datetime('now'), '...', 'owed', '2026-09-09', 'open')"`.

3. **Append to today's journal** at `ledger/journal/YYYY-MM-DD.md`: what you did,
   anything you learned about {THEM}, what you are waiting on. Create the file if
   it does not exist; never edit a previous day.

4. **Return the digest** through the schema.

**Include the open commitments.** `state/commitments.md` and the `commitments`
table hold things {THEY} owe people, deadlines {THEY} {HAVE} accepted, and questions
{THEY} never answered. Do not simply list them — most are not urgent on any given
day. Lead with the ones where today changes something: a deadline getting close,
a dependency that has been stalled long enough to chase, something that becomes
impossible if {THEY} wait. Give the rest a single line of aggregate ("nine
others, nothing moving").

**The point of this is to say what {THEY} do not already know.** Items marked
**NEW** have never been in a digest before. Everything else {THEY} {HAVE} been told at
least once, so it earns a line again only when one of these is true:

- it happens today or tomorrow, and the timing is now the news
- something about it changed — a date moved, a status flipped, a deadline that
  was distant is now close
- it is about to become impossible, or {THEY} are visibly not acting on it

Otherwise summarise the unchanged in a clause and move on: "nine other open
commitments, none moving". A digest that re-reads the same exam three mornings
running is one {THEY} stop reading, and then the one morning it matters {THEY}
will not see it either.

Things far ahead are still worth a mention *once*, on the day they first appear.
That is the whole reason the horizon is wide.

What makes a good digest:

- Lead with what changes the next few hours, not with a summary of the snapshot.
- Deadlines that could still be missed beat deadlines that are far away.
- A person waiting on a reply beats a newsletter, always. The snapshot's
  awaiting-reply list is a starting point, not a verdict — some of it is
  automated mail that slipped the filter, and you should say so rather than
  repeat it.
- Public events from a feed: there are hundreds. Mention one only if there is a
  real reason this person in particular would want it.
- If nothing matters today, say that in one line. A short honest digest is worth
  more than a padded one, and padding is how a digest stops being read.
- **Write to {THEM}, not about {THEM}.** "You have", never "{NAME} has". Every word
  of this reaches a screen {THEY} are looking at.
- The digest goes out in full, so it can breathe — it does not have to fit in a
  notification. But length still has to be earned; a long digest that is mostly
  filler is worse than a short one.

What you must not do: send anything, reply to anyone, post anything, RSVP, or
change anything outside `ledger/`. If you want to do one of those, put it in
`questions` and {THEY} will answer.

If the identity files are still thin, lean on evidence from the ledger rather
than inventing preferences, and say plainly when you are guessing.
"""


def _brain_url() -> str | None:
    try:
        out = subprocess.run([str(config.ROOT / "bin" / "herald-brain"), "url"],
                             capture_output=True, text=True, timeout=20)
        return out.stdout.strip() or None
    except Exception:                                     # noqa: BLE001
        return None


def main() -> int:
    now = dt.datetime.now(config.tz())
    with db.session() as con:
        snapshot, shown = build(con, CYCLE)

    # Keep the snapshot: when a digest is wrong, the question is always whether
    # the agent reasoned badly or was handed bad rows.
    snap_dir = config.RAW / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / f"{now:%Y-%m-%d}-{CYCLE}.md").write_text(snapshot)

    result = think.think(
        f"{INSTRUCTIONS}\n\n---\n\n{snapshot}",
        label=LABEL,
        cwd=config.ROOT,
        json_schema=SCHEMA,
        permission_mode="auto",
        allowed_tools=["Read", "Write", "Edit", "Glob", "Grep",
                       "Bash(herald db *)", "Bash(herald status *)", "Skill"],
    )

    if not result.ok:
        notify.push("Herald: dawn cycle failed",
                    (result.error or "unknown")[:400],
                    priority="high", tags="rotating_light")
        print(result.error, file=sys.stderr)
        return 1

    payload = result.json() or {}
    headline = payload.get("headline") or "Good morning"
    digest = payload.get("digest") or result.text
    priority = payload.get("priority") or "default"
    questions = payload.get("questions") or []

    # The digest is a document; keep it where they can go back to it.
    out_dir = config.LEDGER / "digests"
    out_dir.mkdir(parents=True, exist_ok=True)
    body = [f"# {headline}", f"\n*{now:%A %-d %B %Y, %H:%M}*\n", digest]
    if questions:
        body.append("\n## Waiting on your answer\n")
        body.extend(f"- {q}" for q in questions)
    (out_dir / f"{now:%Y-%m-%d}.md").write_text("\n".join(body))

    # One notification. Telegram carries the whole digest and is where they can
    # reply to it; ntfy fires only if Telegram is unreachable, because a
    # fallback that always fires is just a second buzz.
    body = [f"_{now:%A %-d %B}_", "", digest.strip()]
    if questions:
        body += ["", "*Want me to?*"] + [f"• {q}" for q in questions]
    where = notify.tell(headline, "\n".join(body), priority=priority,
                        tags="sunrise", click=_brain_url())

    # Only now, once it has actually gone out, does any of this count as told.
    with db.session() as con:
        db.mark_reported(con, shown)
        db.mark_actions_reported(con, [a["id"] for a in db.unreported_actions(con)])

    engine = f"{result.engine}/{result.model}"
    cost = f"${result.cost_usd:.4f}" if result.cost_usd is not None else "?"
    print(f"{CYCLE}: {headline}  [{engine} {cost} {result.duration_ms}ms → {where}]")
    if where != "telegram":
        print(f"  digest went to {where}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
