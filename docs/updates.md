# Updates

Herald is one program that several people run. This is how a copy of it stays
current, and what it costs either way.

## The choice, made at setup

Setup asks once, and `herald mode` shows or changes it later.

| | **Follow updates** (default) | **Fork** |
|---|---|---|
| the program | not yours to edit | yours entirely |
| new releases | offered, one tap to install | never again |
| your settings, ledger, extensions | yours, always | yours, always |
| switching later | → fork keeps everything | → following means discarding your changes |

There is a third mode, `maintainer`, for the install that *is* upstream. It
edits, commits and pushes, and `herald release` is what other copies receive.

## Following updates

Once a day, a collector fetches and looks for a release tag newer than the one
this install is on. When it finds one it records it — so the morning digest can
mention it — and asks, once, as a one-tap approval. Nothing is installed until
you say yes.

```bash
herald update --check     # what is available, and what it would change
herald update            # apply it
```

Applying one is a fast-forward to the tagged commit, followed by everything a
running install needs to catch up: dependencies, migrations, a regenerated
constitution, re-linked extension skills, refreshed background jobs, and a
restart that waits for any live conversation to finish. Then it runs
`herald check`, and **if the invariants do not hold afterwards it stops before
restarting anything** and tells you.

Nothing in `$HERALD_HOME` is touched. Not your ledger, not your config, not your
extensions.

### If an update refuses

It will say which of these it is:

- **local changes in the program directory** — something edited the program.
  `git -C <checkout> status` shows what; reverting it lets the update proceed,
  or `herald mode fork` keeps it and stops updating.
- **the tag is not a descendant** — history was rewritten upstream, or this is
  really a fork. Nothing automatic will resolve that, and it should not.

### Why the program is guarded rather than merely discouraged

A tracking install has a `pre-commit` hook that refuses commits in the program
directory, and `herald check` fails if the directory has drifted. Both exist
because the failure is otherwise invisible and delayed: a session that edits one
file leaves an install that can never fast-forward again, and the person running
it finds out weeks later when an update refuses for reasons they cannot connect
to anything they remember doing.

### What to do instead of editing the program

Almost everything worth changing is outside it:

```bash
herald config set <path> <value>    # see config/defaults.json for what exists
herald ext new <name>               # a collector, cycle or skill of your own
```

An extension in `$HERALD_HOME/extensions/` can **override one Herald ships**, by
name — so even replacing a built-in behaviour does not require touching the
program. `docs/extensions.md` is the guide. And
`ledger/identity/constitution.md` is the half of your agent's rules that belongs
to you: how it talks to you, what it must never get wrong, what it may not do.

If what you want genuinely needs a change to Herald itself, open an issue
upstream. That is a shorter path than a fork for anything that would be useful
to more than one person.

## Forking

```bash
herald mode fork
```

The program becomes yours. Commit freely; nothing will offer you an update
again. `herald update --check` still reports what upstream has done, which is
worth reading before fixing something that has already been fixed.

Going back means discarding your changes, so it is a decision rather than an
experiment.

## Maintaining

For the install that is upstream:

```bash
herald release --preview      # what has accumulated since the last tag
herald release                # cut it: version, changelog, tag, push
herald release --minor        # or --major
```

`main` reaches nobody; a tag reaches everybody. A release refuses to go out if
`herald check` fails — which includes the scan that no personal data has entered
the program — or if anything is uncommitted, since a tag has to contain the work
it claims to.

The changelog is built from commit subjects, which is why they are written to
explain a decision rather than to name a diff.

### Changing something an install already has

- **a new setting** — free. `config/defaults.json` is merged *under* the user's
  own config, so a key added upstream simply appears.
- **a new table or column** — nearly free; the schema is `IF NOT EXISTS` and
  columns are added idempotently.
- **a change of shape** — a renamed fact field, a moved directory, a rewritten
  JSON payload — needs a migration in `migrations/`. `lib/herald/migrations.py`
  explains the contract; the short version is that it is numbered, idempotent
  anyway, never destructive without a copy, and says what it did.

A change that only works on the machine it was written on is a change that
breaks everybody else's install silently.
