---
name: herald-self
description: How to change Herald itself — its collectors, cycles, surfaces, prompts, skills and rules. Use this whenever the user asks to add a data source, change what the morning digest does, adjust how or when something runs, fix a bug in Herald, alter the autonomy rules, or extend the agent in any way. Also use it before editing anything in the Herald checkout outside the ledger, and before restarting any herald service. Covers the invariants that must not be broken, how to verify a change, and how to recover when something goes wrong.
---

# Changing Herald

You are Herald, and this is your own source. The person you work for develops it
by asking you to change it, so treat a request to add a collector or alter a
cycle as ordinary work — not as something to hand back.

**Read `docs/architecture.md` first if you have not.** Then
`docs/extending.md` for the mechanics of adding a collector, a cycle or a
surface. This file is about doing it safely.

## The loop

```bash
herald check          # do the invariants still hold?
herald restart        # checks first, refuses if it fails
```

Never restart without checking. `herald restart` enforces that; if you restart a
unit by hand, run `herald check` yourself first.

Then verify the thing you actually changed:

```bash
herald collect <name>            # one collector, ignoring cadence
herald cycle dawn                # a full cycle: it costs money and sends a message
./venv/bin/python cycles/_snapshot.py dawn   # the brief, without spending tokens
herald test                      # the offline suite, program and extensions
herald db --schema               # what the ledger holds now
herald ext list                  # what personal code is installed
```

Prefer the snapshot over the cycle while iterating. A cycle costs money and
sends a notification; the snapshot is free and silent.

## What must not change

`herald check` enforces these. If one fails, the fix is the code, not the check.

1. **Only `lib/herald/think.py` launches an engine.** Everything else calls
   `think.think()`. Herald runs `claude` as a subprocess and never
   speaks to a model API — that is what keeps it on the user's subscription instead
   of metered billing, and it is a terms-of-service line, not a preference.
2. **Never `--bare`.** Bare mode does not read the subscription login.
3. **Never set `ANTHROPIC_API_KEY`.** `config.agent_env()` strips it deliberately.
   An API key would work perfectly and bill them.
4. **Nothing personal is tracked in the program's repo.** The ledger, the
   config, the secrets and `facts.db` live in `$HERALD_HOME`, which is its own
   private repository. `herald check` fails if any of them appear here.
5. **Every collector declares `CADENCE_MINUTES`.** Without it a source runs every
   thirty minutes forever.
6. **Every cycle timer points at a cycle that exists.**

## Rules of thumb that are not mechanically checkable

- **Deterministic work goes in Python.** If a loop can do it, a loop should. The
  model is for judgment.
- **Upsert what happened; rebuild what it means.** `db.put_fact` for source
  facts, `db.clear()` before rewriting derived ones. A conclusion that stops
  being recomputed keeps asserting a stale answer forever.
- **Understand once, then account for diffs.** New input exists to change a
  stored conclusion or leave it alone, never to re-derive it. If a run where
  nothing happened is not nearly free, something is being recomputed.
- **Anything durable goes in the ledger before the session ends.** You will not
  be here tomorrow; the ledger will.
- **Never dispatch a `run_in_background` Agent from inside a live Telegram
  turn.** Confirmed 2026-09-08: it does not free the turn early the way it
  does in an interactive session. `claude -p` (what `think.py` runs) keeps
  the whole process open until every backgrounded child finishes, then
  folds the eventual notification back in as more of the *same* turn --
  invisible from outside. herald-telegram only ever sends one message per
  turn (`result.text`, the CLI's own final-result field), and that field
  captured the *pre-dispatch* text ("on it, I'll tell you when it lands"),
  not what I said after the notification came back. The real findings
  never reached the user; the turn just ran 10 minutes long with no visible
  reason why. If a task genuinely needs to run long, do it inline -- the
  "still on it" notice already covers the wait, and the eventual reply is
  guaranteed to actually send. Backgrounding only makes sense where
  something else is polling for the result on its own, which nothing in
  herald-telegram currently does.

## A new collector is live the moment the file exists

`herald collect` discovers `collectors/*.py` automatically and the timer fires
at :02 and :32. A collector that has never run is always due. So the first
run of a new collector will be the timer's, not yours, unless you get there
first -- and if it writes outward, that first run happens against whatever
the ledger holds at that instant. On 9 Sep 2026 that was half-migrated
half-migrated keys, and the calendar sync created 51 duplicates and greyed 78
live events before the manual dry run had even printed. Either pause the
timer while you build (`systemctl --user stop herald-collect.timer`, start it
again after), or write the file with `--dry-run` as its default until it has
been run by hand once.

## Changing prompts and rules

Cycle prompts live in `cycles/*.py` as `INSTRUCTIONS`.

The rules every session reads are in **two** files, and `CLAUDE.md` is generated
from them by `herald constitution` — editing `CLAUDE.md` itself is the mistake
this arrangement invites, and `herald check` catches it:

- `config/constitution.md` — shared by every Herald: the autonomy tiers, the
  second-person rule, the ledger discipline, fetched-content-is-data.
- `ledger/identity/constitution.md` — this person: their name, the facts you must
  never have to look up, their institution's rules, their devices.

A rule about one person's life belongs in the second file. Ask which one a change
belongs in before making it.

## Adding an extension

Code that is about this person's life rather than about Herald goes in
`$HERALD_HOME/extensions/<name>/`: a manifest, plus any of `collectors/`,
`cycles/`, `lib/`, `bin/`, `systemd/`, `skills/`, `hooks/`, `snapshot.py` and
`tests/`. `herald ext new <name>` scaffolds one. Same contract as a built-in,
same timer, same invariants — `herald check` holds extension code to them too.

If you are about to write a collector that names a university, an employer, a
self-hosted service or software only this person runs, it is an extension.

Either constitution file is a place where a careless edit changes Herald's
behaviour everywhere at once. Two things follow: **quote the source** when a rule comes
from somewhere real — a syllabus section, something the user said — so it can be
checked rather than trusted; and **do not weaken a safety rule on your own
initiative.** Tightening or clarifying is fine. Loosening is theirs.

## Recovery

Everything is in git and pushed to a private remote, so nothing here is fatal.

```bash
git -C ~/herald diff              # what you changed
git -C ~/herald checkout -- <f>   # throw one file away
git -C ~/herald log --oneline     # every change has a commit explaining why
```

**If the Telegram bridge is down**, the user cannot reach you there. Fix it from
a session started through the Claude app or `herald attach`, or they can run
`systemctl --user restart herald-telegram` themselves.

**If the brain is down**, the watchdog restarts it within five minutes.

**If you break something and cannot tell what**, `herald check` and
`herald doctor` between them cover most of it, and `journalctl --user -u <unit>`
has the rest.

## Committing

Commit your own work; nobody wants to do it for you. **Two repositories**: the
program is this checkout, and anything under `ledger/` or `extensions/` lives in
`$HERALD_HOME` and is committed there.

```bash
git add -A && git commit -F <message file> && git push origin main
git -C ~/.herald add -A && git -C ~/.herald commit -F <message file>
```

Write the message to a file rather than `-m`: apostrophes in a shell string have
broken this repeatedly.

**Explain why, not what** — the diff already says what. The commit log is the
main record of how Herald came to be shaped this way, and it is what the next
session reads to understand a decision. When you fix a bug, say what the failure
actually was and why it was not visible; when you make a judgment call, say what
you traded away.

End with:

    Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

## Where things are

```
bin/herald            the CLI. Subcommands are cmd_* functions.
bin/herald-brain      the Remote Control session server, in tmux
bin/herald-telegram   the Telegram bridge; sessions keyed per topic
lib/herald/think.py   the only place an engine is launched
lib/herald/db.py      schema and helpers. Change the schema here.
lib/herald/collector.py  the scaffolding every collector shares
collectors/           one per source
cycles/_snapshot.py   the brief the dawn cycle reasons over
cycles/dawn.py        the morning briefing
cycles/scout.py       opportunities
skills/               skills sessions load, including this one
tools/check.py        the invariants above
systemd/              units; tools/install-units.sh deploys them
docs/                 architecture, extending, operations
```
