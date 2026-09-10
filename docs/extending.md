# Extending Herald

## Adding a collector

A collector reads one source and writes facts. **No model calls, ever** —
ingestion runs every 30 minutes and must cost nothing.

```python
#!/usr/bin/env python
"""One paragraph on what this reads and why it is worth having."""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))
from herald import collector, db

NAME = "spotify"
REQUIRES = ("spotify",)      # capabilities this needs; see below
CADENCE_MINUTES = 360        # how often this is worth running

def collect(con) -> dict:
    for item in fetch():
        db.put_fact(con, NAME, "play", external_id=item["id"],
                    ts=item["played_at"], title=item["track"],
                    data={"artist": item["artist"]})
    return {"plays": n}       # counts, printed after the run

if __name__ == "__main__":
    collector.main(NAME, collect)
```

Drop it in `collectors/`. It is discovered automatically — no registration.

**If it names a university, an employer, a self-hosted service or software only
one person runs, it is an extension**, not a built-in collector. Same contract,
different directory: see [`extensions.md`](extensions.md).

### Declaring what it needs

`REQUIRES` names capabilities. A capability is available when the user enabled
it *and* the credential it names exists, and a collector whose requirements are
unmet is skipped with the reason rather than failing — "you never set this up"
and "this is broken" want different responses. Built-in capabilities live in
`lib/herald/capabilities.py`; an extension declares its own in its manifest.

### The rule that is easy to get wrong

> **Upsert what happened. Rebuild what it means.**

Source facts (a message, an event, a posting) are immutable history: `put_fact`
upserts them on `(source, kind, external_id)`, so re-reading a window is safe.

Derived facts are conclusions — "this thread is waiting on a reply", "these are
the current courses". A conclusion that stops being recomputed keeps asserting
yesterday's answer forever. Call `db.clear(con, NAME, kind)` before rewriting
those.

Two live examples of getting it wrong: 22 email threads kept claiming they were
awaiting a reply after the run that concluded otherwise; and a batch of rows
outlived the collector version that wrote them, because `db.clear` only runs for
kinds a collector still writes — **dropping a kind orphans its rows.**

### Incremental where the source allows it

Return `_cursor` in the counts dict and it is stored in
`collector_state.cursor`; read it back with `collector.cursor(con, NAME)`.

- `gmail` uses the history API — 4.0s and 222 messages became 0.9s and zero.
- `gcal` uses `updatedMin`. A sync token would be the obvious tool, but Calendar
  refuses those alongside `timeMin`/`timeMax`, and the bounded window is worth
  more here.

Always keep a full-resync fallback for when the stored position ages out.

### Failure handling is free

`collector.run` does it: a failure streak is tracked, the user is told after two
consecutive failures, and told again when it recovers. Just raise.

## Ingesting a one-off document set

Not everything is a live source worth a collector. Sometimes someone hands over a
folder that will never change again -- college decision letters, a scanned
form, a batch of PDFs -- and the job is just to get the text into `facts.db`
once. That's `tools/ingest_documents.py`, not a new collector:

```
tools/ingest_documents.py ~/College/Decisions --source college --kind decision
```

It walks the folder, runs `pdftotext`/`tesseract` per file (caching the
extracted text under `ledger/raw/ingest/<source>/`, so a re-run is free unless
the source file changed), and upserts one fact per file keyed on its relative
path. Read the module docstring before reaching for it again -- it explains
exactly how the date it guesses for `ts` is found and where that heuristic is
known to be wrong, rather than leaving that as something to rediscover.

## Coordinates and addresses

`lib/herald/geo.py` is the one place that turns a coordinate into an address
or an address into a coordinate — read its module docstring before writing
another haversine or another Nominatim call anywhere else. Three layers,
cheapest first: the hand-maintained gazetteer at `config/secrets.json` ->
`"places"`, a permanent cache at `ledger/raw/geocode-cache.db`, then the
network (OpenStreetMap's public Nominatim, rate-limited to 1 req/s inside the
module so callers never need their own throttle). Anything that geocodes sends
the query to OpenStreetMap, with the user's own contact address in the
User-Agent as Nominatim's policy asks — worth knowing before enabling
something that does it in bulk.

## Writing to Google

Never call a mutating Calendar, Tasks or Gmail method from a collector or a
cycle. Call `lib/herald/gwrite.py` -- `calendar_insert`, `calendar_update`,
`calendar_cancel`, `task_create`, `gmail_label`, `gmail_draft` -- with an
`actor` string, and it logs the write to the `actions` table so the next
digest can report it. `herald check` fails on any other path.

A sync that keeps a calendar equal to a source does not need writing: that is
`lib/herald/calsync.py`. Build the desired set in Python, then
`calsync.plan()` / `calsync.guard()` / `calsync.apply()`. It stamps identity
onto events with `sync_key` so the next run recognises its own work, adopts
matching events it does not own instead of duplicating them, keeps hand edits,
cancels rather than deletes, and refuses a pass that wants to remove more than a
cap. The planner is pure and has tests.

## Adding a cycle

A cycle is the only place a model is invoked on a schedule. The shape:

```
collect (already done, its own timer)
  → snapshot     deterministic Python, free
  → ONE session  claude -p via think.think()
  → apply        deterministic Python
  → notify
```

One session per cycle, not one per concern. The snapshot is what makes that
affordable: the agent gets a page of curated rows, so a cycle costs about the
same whether the ledger holds two thousand facts or two hundred thousand.

```python
result = think.think(prompt, label=f"cycle:{CYCLE}", cwd=config.ROOT,
                     json_schema=SCHEMA, escalate=busy,
                     permission_mode="auto",
                     allowed_tools=["Read", "Write", "Bash(herald db *)", ...])
```

- `label` shows up in the `runs` table — use `cycle:<name>` so spend is
  attributable.
- `escalate=True` picks Opus. Decide it from how much is genuinely new, not by
  default: scout's first bulk pass cost $2.79 and the same pass after the diff
  rewrite cost $0.14.
- Let the session write the ledger itself; those are green actions and it has
  the tools. Return through `json_schema` only what Python must deliver.
  **Nothing that leaves the machine is left to prose.**

Add `systemd/herald-cycle-<name>.timer` with `Unit=herald-cycle@<name>.service`
— the unit files are templates, `{{ROOT}}` and `{{GOOGLE}}` filled in at install
time — then `herald services install` and `herald services enable`. `herald
check` verifies the timer and the cycle file match; two timers once pointed at
cycles that were never written.

On macOS the equivalent is a launchd agent, generated in `setup/services.py`
rather than templated, because a plist is XML and a schedule is a different
shape there.

## Adding a surface

`notify.tell(title, body)` reaches the user once: Telegram first, ntfy only if
Telegram is unreachable. Do not call `notify.push` directly unless the point is
specifically to route around Telegram.

## Things that will bite

- **Telegram allows one `getUpdates` consumer per bot.** Anything else gets 409.
  `herald-telegram` holds the poll, so chat-id discovery lives there and
  `notify` only reads it.
- **Authorise on the sender, not the chat.** A group has a different chat id
  than a DM.
- **`PYTHONUNBUFFERED=1`** on any long-running service, or its logs sit in a
  buffer forever and the thing looks dead while working.
- **Do not pipe a TUI into `tee`.** It makes stdout a non-TTY, Claude Code
  concludes it is being scripted and switches to `--print` mode. Use
  `tmux pipe-pane`.
- **`pkill -f` matches its own command line.** Use a bracket: `'foo[b]ar'`.
- **`RETURNING` leaves a cursor open**; drain it before `commit()`.
- **`rsync --delete` into `~/herald`** will remove `.git`, `facts.db` and
  `logs/`. Work on the server; it is the source of truth.
