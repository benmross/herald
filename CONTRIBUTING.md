# Contributing

Herald is one person's agent that turned out to be worth sharing. Contributions
are welcome; the bar is that a change should be true for everyone who installs
it.

## Before anything else

```bash
herald check     # the architecture's invariants
herald test      # the offline suite, program and extensions
```

Both are fast, neither touches the network, and `herald check` is what stops a
change from silently breaking something that fails quietly.

## Where a change belongs

**If it names a university, an employer, a self-hosted service, or software only
you run, it is an extension** — `$HERALD_HOME/extensions/`, documented in
[`docs/extensions.md`](docs/extensions.md). This is the single most common
mistake and the reason the restructure happened: an architecture that knows one
university's event-feed ids has decided its users are students there.

If it is genuinely general, it belongs in the program.

## The invariants

`tools/check.py` enforces these; if one fails, the fix is the code:

1. **Only `lib/herald/think.py` launches an engine**, and it launches the
   `claude` CLI. Never a model HTTP API, never an SDK, never `--bare`, never an
   API key. This is a terms-of-service line, not a preference.
2. **Only `lib/herald/gwrite.py` writes to Google**, so every outward write is
   logged and the digest can report it.
3. **Every collector declares `NAME`, `CADENCE_MINUTES`, `REQUIRES` and
   `collect()`**, and calls no model.
4. **Nothing personal is tracked** in this repository. The check derives its
   denylist from the running install's own config and secrets.
5. **`CLAUDE.md` is generated.** Edit `config/constitution.md` (shared) or the
   personal one in the ledger, then `herald constitution`.

## Style

The code is commented for a reader who has to change it under pressure, often an
agent with no memory of how it came to be. That means:

- **Say why, not what.** The diff already says what.
- **When something is the way it is because of a specific failure, say which
  failure.** Half the comments here are a bug someone had first, and they are the
  most useful half.
- **Commit messages explain the decision**, including what was traded away. The
  log is the main record of how this got its shape.

## Tests

Offline, fast, no network, no model, no messages sent. Add one where a mistake
would otherwise be silent: parsing, diffing, gating, schema.

## What is unlikely to be merged

- A second model provider. There is one engine on purpose; see
  "One engine, no fallback" in [`docs/architecture.md`](docs/architecture.md).
- A dependency that a small standard-library amount of code would cover.
- Anything that makes the agent act outward more easily without also making it
  more visible — the amber tier's whole value is that it is never silent.
