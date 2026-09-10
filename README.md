# Herald

A personal agent that runs on your own machine.

It reads what you connect (your mail, your calendars, your tasks, whatever
feeds you point it at), keeps a durable record of what it concludes, briefs you
each morning on what actually matters that day, and interrupts you when
something real is closing. You talk to it on Telegram, in a terminal, or through
the Claude app, and it is the same agent every time, because its memory is a
directory of files rather than a conversation.

It is also yours to shape by asking. "Read my library's events feed too", "stop
telling me about X", "send the digest at seven". It writes the setting, or the
extension, or the rule, itself.

Setup asks once whether your copy should **follow updates** (the default: it
offers each new release as a one-tap install, and treats the program as not its
own to edit) or **own its program** (it can rewrite any part of itself, and
receives no further updates). See [`docs/updates.md`](docs/updates.md).

```bash
curl -fsSL https://raw.githubusercontent.com/benmross/herald/main/install.sh | bash
herald setup --web
```

**[docs/install.md](docs/install.md)** is the full walkthrough, written for
someone who has not used a terminal before. Setup takes about half an hour, most
of which is you writing about yourself.

## What it costs, and what it needs

Herald runs the `claude` CLI as a subprocess on **your own Claude subscription**.
It never calls a model API and never uses an API key, so there is no per-token
bill: a normal day is a handful of invocations. It needs a Mac or a Linux
machine that is on when you want it working, a Google account, and about half an
hour.

"On when you want it working" is the part worth thinking about, because Herald
is most useful always-on. An old laptop or a Raspberry Pi in a cupboard is the
best answer for most people; Oracle Cloud's Always Free tier is the only cloud
free tier with enough memory, and it has real caveats.
[`docs/hosting.md`](docs/hosting.md) compares them and covers setting up over
SSH.

## The idea

Most tools that read your calendar can tell you what is on Thursday. Almost none
can tell you which of the four things on Thursday matters, because that depends
entirely on what you are trying to do and what you are like. Herald asks you, at
length, during setup, and then everything it ever surfaces is ranked against
what you said.

The other half is that it **remembers what it decided**. A conclusion is written
down and then maintained, never recomputed, so a morning where nothing happened
costs almost nothing and a morning where something changed says what changed.

## What it can read

Out of the box: Gmail, Google Calendar, Google Tasks, Contacts, your GitHub
activity, any published `.ics` calendar feed, and public job and internship
postings. On a Mac it can read your Messages database and tell you who is
actually waiting on a reply.

Anything else is an **extension**: a directory with a manifest, discovered
automatically, holding collectors, cycles, skills and background jobs of its
own. Extensions are where anything specific to one person's life belongs: a
university's event scraper, a self-hosted service, a data source only you have.
See [docs/extensions.md](docs/extensions.md).

## What it will and will not do without asking

Three tiers, and the middle one is the one people are surprised by:

- **It acts freely** when nothing leaves the machine: reading, searching,
  writing to its own notes, drafting something for you to look at.
- **It acts and then tells you** for reversible things only you see: putting an
  event it found on a calendar it manages, labelling mail, opening a task. Never
  silently: every one appears in the next digest.
- **It asks first, every time**, for anything anyone else sees: sending a
  message, posting, applying, registering, spending money, deleting anything
  permanently.

Being asked to *look* at something is never permission to *change* it. The rules
are in [`config/constitution.md`](config/constitution.md), which every session
reads, and enforcement of the amber tier is mechanical: writes to Google go
through one module that logs each one, and the morning digest reads that log.

## Where your data lives

Two directories, and the separation is the point:

```
the checkout        the program. Public, identical for everyone.
$HERALD_HOME        you. Private, yours, ~/.herald by default:
                    config, credentials, and the ledger: identity, state,
                    journal, and the database of everything ingested.
```

Nothing personal is ever committed to the program's repository, and `herald
check` fails if anything is. Your ledger is yours to version, back up, read, or
delete; nothing is sent anywhere except to the services you connected and to
Claude Code, which is what you are already using.

## Staying current

```bash
herald update --check     # what a new release would change
herald update             # apply it: fast-forward, migrate, restart
herald mode               # follow updates, or own the program
```

An install that follows updates checks daily and asks before installing
anything. Nothing in your own directory is touched by an update: not your
ledger, not your settings, not your extensions.

## Reading further

- [`docs/install.md`](docs/install.md): setting it up
- [`docs/hosting.md`](docs/hosting.md): where to run it, free options compared
- [`docs/architecture.md`](docs/architecture.md): what it is and why it is
  shaped this way
- [`docs/extending.md`](docs/extending.md): adding a collector, a cycle, a surface
- [`docs/extensions.md`](docs/extensions.md): packaging your own
- [`docs/updates.md`](docs/updates.md): following releases, forking, releasing
- [`docs/operations.md`](docs/operations.md): running it, and what to do when
  something breaks
- [`SECURITY.md`](SECURITY.md): what it stores, what it never sends, and how
  the prompt-injection surface is handled

## The rule that shapes everything

Herald runs the official `claude` CLI as a subprocess. It never calls a model
HTTP API. That is what keeps it billed to a subscription rather than API credit,
and `lib/herald/think.py` is the only module allowed to launch an engine.

## Layout

```
bin/herald            the one CLI
bin/herald-brain      the persistent session server
bin/herald-telegram   the Telegram bridge
lib/herald/           config, db, notify, think, collector, gwrite, calsync
collectors/           one module per source. Pure Python, no model calls.
cycles/               snapshot → one session → apply
setup/                the wizard: one engine, a terminal and a browser frontend
extensions/           optional extras Herald ships, off by default
skills/               what a session loads: the ledger, itself, Google
config/constitution.md  the rules every session reads
systemd/              unit templates
docs/                 this
```

MIT licensed.
