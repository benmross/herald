# Extensions

An extension is the part of Herald that is about *your* life rather than about
Herald: a university's event scraper, a self-hosted service, a data source only
you have, a skill for querying something only you run.

They exist because of what happened without them. Herald began as one person's
agent and its collectors knew one university's event-feed ids, which meant the
architecture assumed, without anyone having decided it, that its users were
students there. Anything that
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
  bin/                     its commands; each gets a launcher in ~/.local/bin
  systemd/                 unit templates, installed with Herald's; {{ROOT}} is
                           the checkout and {{HOME}} is $HERALD_HOME
  skills/<name>/SKILL.md   symlinked where sessions load them
  hooks/hooks.json         shared hooks, synced into both CLIs' hook settings
  red.py                   red actions of its own, behind the same tap
  telegram_commands.py     deterministic slash commands for ledger queries
  tests/                   run by `herald test`
```

Every directory is optional. Most extensions are a manifest and one collector.

## Telegram query commands

An enabled extension can add slash commands without starting a model. Put
`telegram_commands.py` beside its manifest:

```python
def tasks(con, argument):
    rows = con.execute(
        "SELECT title FROM facts WHERE source='gtasks' AND kind='task' "
        "AND json_extract(data,'$.status')='needsAction' ORDER BY ts, title"
    ).fetchall()
    return "\n".join(row["title"] for row in rows) or "You have no open tasks."

COMMANDS = {"/tasks": tasks}
```

The handler receives a query-only SQLite connection and the text following the
command, and returns plain text. `/tasks@your_bot today` passes `today`. Your
reply appears in the topic that asked. The bridge checks the sender against
your configured owner before invoking a handler. Exceptions produce an error
reply and never fall back to a model turn.

Names follow Telegram's lowercase command convention and cannot replace a
built-in command or `/compact`. If two enabled extensions claim the same name,
neither gets it. A broken import affects only its extension. Commands are
loaded once when the bridge starts; restart it after changing or enabling an
extension. They are unavailable inside a private conversation because their
code runs outside its sandbox.

This interface is for queries. Handlers must not start models, ask for taps,
or write files or services. It supplies no callback registration, so an
extension cannot intercept `approve:` or root callbacks through it. The
query-only connection catches accidental database writes; installed extension
code remains trusted Python and can open other connections. It is not a
sandbox for untrusted code.

```bash
herald ext new tides          # scaffold one
herald ext list               # what is installed, and what each provides
herald ext enable tides       # links its skills, merges its hooks
herald ext disable tides
```

Herald also ships a few of its own under `extensions/` in the checkout. Those
are off until you turn them on, and enablement is stored in *your* config rather
than in their manifest, so a `git pull` can never switch one on.

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

A capability is how a collector says what it needs. `secrets`, `settings` and
`files` are checked before anything runs, so a source you have not configured is
reported as *off* rather than failing every half hour at a credential nobody
asked you for.

The manifest is read, never imported. Asking an extension what it provides must
never mean running its code.

## A collector

It has the same contract as a built-in one, plus a four-line bootstrap so it
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
holds extension code to them exactly as it holds Herald's own: no model calls in
a collector, no engine launched outside `think.py`, no write to Google outside
`gwrite.py`. Those are the failures that are silent, and a personal collector is
as able to cause them as a built-in one.

## A red action of its own

Anything another person sees, that costs money or that cannot be undone goes
through `lib/herald/red.py`, and an extension is held to that too. Ship a
`red.py` beside the manifest:

```python
def _render(p: dict) -> str:      # what the user is asked, built from the payload
    return f"Post this to the board?\n\n{p['text']}"

def _perform(p: dict) -> tuple[str, str]:   # runs once, only after their tap
    post_id = board.post(p["text"])
    return (f"posted to the board", f"board:{post_id}")

KINDS = {"tides.post": (_render, _perform)}
```

A kind must be named `<extension>.<verb>` and cannot replace a built-in one.
`herald act run tides.post --payload-file p.json` asks and then acts, and
`--show` prints what would be asked. The function that does the writing can
call `red.acting()` and refuse when it returns None, so that nothing reaches
it except through an approval.

## Appearing in the morning brief

The cycle snapshot queries by *shape* and never by source, so a collector that writes
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

A section that raises is left out, and the rest of the brief is still built.

## Sharing one

An extension is a directory, so it can be a git repository of its own, and
that is how one is shared. Somebody else installs it with

```bash
herald ext install https://github.com/someone/herald-tides
herald ext setup tides
herald ext enable tides
```

`install` clones it into `$HERALD_HOME/extensions/`, prints what it would
run (collectors, cycles, skills, units, hooks, red actions) and which settings
and secrets its manifest says are still unset, and leaves it **off**. A
collector is live the moment its file exists, so without that the first run
of somebody else's code would be the timer's, before anyone had read what it
does. It records the URL as `extensions.<name>.source` in config.json, and
adds the directory to `$HERALD_HOME/.gitignore` so the ledger's repository
does not swallow the clone as an embedded repo.

`herald ext update` fast-forwards every installed extension that is its own
repository, or one by name. It refuses a clone that has been edited, for the
same reason a tracking install of Herald cannot be edited: a fast-forward
cannot happen over a local change.

So **nothing about the person goes in an extension's files.** Their values go
in their own config.json, under the extension's name, and the manifest
declares them in a capability's `settings` so that `install` and `herald
status` can ask for them:

```json
"provides_capabilities": [{
  "key": "shop",
  "settings": ["shop.sizes"],
  "setup_hint": "herald config set shop.sizes.tops M"
}]
```

A manifest can also describe each value, so whoever installs it is asked a
real question rather than shown a key:

```json
"setup": [
  {"key": "canvas.base_url", "prompt": "Your school's Canvas address",
   "example": "https://umd.instructure.com"},
  {"key": "canvas.auth_dir", "prompt": "Where the session is kept",
   "default": "{HOME}/canvas"},
  {"key": "canvas.metadata_only_courses", "type": "list", "required": false,
   "prompt": "Course codes to lock to dates and grades"}
]
```

`steps` lists what only the user can do, in order, as plain sentences
Herald reads out one at a time: create a calendar, install a package, log in
once. `herald ext check` refuses an extension with no `setup` list (`[]` when
it needs nothing) or a setting with no prompt, because Herald is the one
asking and a bare config key is not a question.

```json
"steps": [
  "Create a Google calendar named UMD Campus Events (Settings > Add calendar).",
  "Install the scrapers' packages: ~/herald/venv/bin/pip install beautifulsoup4 lxml"
]
```

A `default` is written into the user's config.json at install (and after an
update, for a setting a new version adds), with `{HOME}` and `{ROOT}`
expanded; a value the user already set is never replaced. `herald ext setup
<name>` asks the rest at a terminal. With `--json` it prints them, which is how
a session installs an extension from Telegram: it asks the questions in chat
and saves each answer with `herald config set`. A value marked `"secret":
true` is collected with `herald secret request`, never in chat. An older
manifest that only lists keys under a capability's `settings` is asked about
too, by key.

Code reads them with `config.get("shop.sizes")`, or straight from
`$HERALD_HOME/config.json` if it runs outside Herald's venv. A skill tells the
session where to look (`herald config get shop.sizes`) rather than stating
the answer. Judgement criteria too long for a setting, such as what makes a
campus event worth keeping, go in the person's `identity/preferences.md`, and
the extension's prompt points at the section.

Before publishing one, run

```bash
herald ext check tides
```

It scans every file the extension's repository tracks with the same scan
`herald check` runs on the program: this install's name, email, school, home
paths, account name and every value in secrets.json, plus the shapes of
common credentials. It also wants a manifest with a name, description and
version, and a README. It cannot see a personal fact that is not in your
config (a family member's name, a store's rewards programme), so read the
diff as well.
