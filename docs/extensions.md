# Extensions

An extension is the part of Herald that is about *your* life rather than about
Herald. A university's event scraper, a self-hosted service, a data source only
you have, a skill for querying something only you run.

They exist because the alternative is worse. Herald began as one person's agent
and its collectors knew one university's event-feed ids, which meant the
architecture had quietly decided its users were students there. Anything that
specific now lives outside the program, in the same private directory as the
ledger, and the program stays general.

## The shape

```
$HERALD_HOME/extensions/<name>/
  herald-extension.json    the manifest
  collectors/*.py          discovered by `herald collect`, same contract
  cycles/*.py              runnable by `herald cycle`
  snapshot.py              a section in the cycle brief
  lib/                     importable by its own code
  bin/                     long-running surfaces of its own
  systemd/                 unit templates, installed with Herald's
  skills/<name>/SKILL.md   symlinked where sessions load them
  hooks/hooks.json         Claude Code hooks, merged into .claude/settings.json
  tests/                   run by `herald test`
```

Every directory is optional. Most extensions are a manifest and one collector.

```bash
herald ext new tides          # scaffold one
herald ext list               # what is installed, and what each provides
herald ext enable tides       # links its skills, merges its hooks
herald ext disable tides
```

Herald also ships a few of its own under `extensions/` in the checkout. Those
are off until you turn them on, and enablement is stored in *your* config rather
than in their manifest — so a `git pull` can never switch one on.

## The manifest

```json
{
  "name": "tides",
  "description": "Tide times for the beach, from the national service's feed.",
  "version": "0.1.0",
  "provides_capabilities": [
    {
      "key": "tides",
      "title": "Tide times",
      "summary": "So a 6am surf is a real plan rather than a guess.",
      "secrets": ["tides.api_key"],
      "settings": ["tides.station"],
      "setup_hint": "Get a station id from the service's map page."
    }
  ]
}
```

A **capability** is how a collector says what it needs. `secrets`, `settings` and
`files` are checked before anything runs, so a source you have not configured is
reported as *off* rather than failing every half hour at a credential nobody
asked you for.

The manifest is read, never imported. Asking an extension what it provides must
never mean running its code.

## A collector

Exactly the same contract as a built-in one, plus a four-line bootstrap so it
can find Herald from outside the checkout:

```python
#!/usr/bin/env python
"""One paragraph on what this reads and why it is worth having."""
import os, pathlib, shutil, sys

_EXT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_EXT / "lib"))
_root = os.environ.get("HERALD_ROOT")
if not _root and (_herald := shutil.which("herald")):
    _root = str(pathlib.Path(_herald).resolve().parents[1])
if _root:
    sys.path.insert(0, str(pathlib.Path(_root) / "lib"))

from herald import collector, config, db

NAME = "tides"
REQUIRES = ("tides",)
CADENCE_MINUTES = 360

def collect(con) -> dict:
    for entry in fetch():
        db.put_fact(con, NAME, "tide", external_id=entry["id"], ts=entry["at"],
                    title=entry["kind"], data={"height_m": entry["height"]})
    return {"tides": n}

if __name__ == "__main__":
    collector.main(NAME, collect)
```

The rules in [`extending.md`](extending.md) apply unchanged, and `herald check`
holds extension code to them exactly as it holds Herald's own — no model calls in
a collector, no engine launched outside `think.py`, no write to Google outside
`gwrite.py`. Those are the failures that are silent, and a personal collector is
no less able to cause them.

## Appearing in the morning brief

The cycle snapshot queries by *shape*, not by source, so a collector that writes
a familiar kind shows up with no further work:

| kind | meaning |
|---|---|
| `assignment` | something with a deadline |
| `thread` | a conversation; `data.awaiting_reply` if the last word was not theirs |
| `current` | where the user is now |
| `day` | one row per day of somewhere they were |
| `health` | the source reporting on its own freshness |

For anything that does not fit a shape, ship a `snapshot.py`:

```python
ORDER = 15          # lower comes earlier in the brief

def section(con, ctx) -> str | None:
    rows = ctx.rows("SELECT * FROM facts WHERE source='tides' AND kind='tide'"
                    " AND substr(ts,1,10) = ? ORDER BY ts", (ctx.today.isoformat(),))
    if not rows:
        return None
    return "## Tides today\n" + "\n".join(
        f"- {r['ts'][11:16]}  {r['title']}" for r in rows)
```

A section that raises costs itself, not the brief.

## Sharing one

An extension is a directory, so it is a git repository. Clone it into
`$HERALD_HOME/extensions/` and enable it.

Two things to check before you publish one: that its manifest names every
credential it needs (so someone else's `herald status` tells them what to set
up), and that nothing personal is in it. Herald's own check will not scan a repo
it does not know about.
