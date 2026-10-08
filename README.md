# Herald

A personal agent that runs on your own machine, on your own Claude
subscription.

Herald reads your mail, calendars and tasks, keeps a written record of what it
concludes, sends you a digest each morning, and messages you when a deadline
is about to close. You talk to it on Telegram, in a terminal or in the Claude
app. Its memory is a folder of files on your disk, so it is the same agent
everywhere.

| | |
|---|---|
| Reads | Gmail, Google Calendar, Tasks and Contacts, GitHub activity, any `.ics` feed, public job postings, and iMessage on a Mac. |
| Remembers | Conclusions are written to a ledger and updated when something changes. A morning where nothing happened costs almost nothing. |
| Ranks | Setup interviews you about what you are trying to do. Everything it surfaces is judged against your answers. |
| Reaches you | A digest at 06:30, and a message the hour it finds something closing tomorrow. |
| Asks first | It never sends, posts, applies, spends or deletes without your tap on the exact text. |
| Changes itself | "Read this feed too", "send the digest at seven", "stop telling me about X". It writes the setting or the extension. |
| No API bill | It runs the official `claude` CLI (or `codex`) as a subprocess. No API key, no per-token cost. |

## Quick start

You need a Claude subscription, a Google account, and a Mac or Linux machine
with 4 GB of RAM. Windows works through WSL2 or Docker.

```bash
curl -fsSL https://raw.githubusercontent.com/benmross/herald/main/install.sh | bash
herald setup --web
```

The first line installs Herald and the Claude Code CLI and asks before it uses
sudo. The second prints a link to a setup page in your browser. Setup takes
about half an hour, most of it you writing about yourself.
[`docs/install.md`](docs/install.md) is the full walkthrough, written for
someone who has not used a terminal.

### Docker

```bash
git clone https://github.com/benmross/herald herald-docker && cd herald-docker
docker compose -f docker/compose.yml up -d --build
docker exec -it herald claude auth login
docker exec -it herald herald setup --web
```

The container has no sudo and sees one volume, so a command that goes wrong
cannot touch the rest of your computer. On Windows, Docker Desktop keeps it
running with no window open. [`docs/docker.md`](docs/docker.md) covers what
the container does and does not protect.

## Using it

```bash
herald status       # what it has read, what it cost, what is failing
herald attach       # a conversation in this terminal
herald brain url    # a link that opens the same agent in the Claude app
herald doctor       # find what is broken and how to fix it
herald update       # install the newest release
herald setup        # re-run any setup step
```

In Telegram, each forum topic is its own conversation. `/new` starts a topic
over, and replying to an older message picks that conversation back up.
`/models` lists both CLIs' available models and lets you switch by tapping a
button or sending `/models engine/model`. Switching models within an engine
keeps the conversation; switching engines starts a fresh session.
After choosing a model with `/models`, pick one of its supported thinking levels.
Your choices appear in the original picker message, which removes the buttons
when you finish.
`/effort` reopens that picker; its CLI default button clears the override.
`/opus`, `/sonnet` and `/codex` also choose the model for a topic, `/stop` cancels
a running turn, and `/obligations` lists what you owe.

## What it does without asking

| | |
|---|---|
| Does it | Anything that stays on the machine: reading, searching, writing its own notes, drafting. |
| Does it, then tells you | Reversible things only you see: adding an event to a calendar it manages, labelling mail, opening a task. Each one is in the next digest. |
| Asks every time | Anything another person sees, anything that costs money, anything permanent. |

The rules are in [`config/constitution.md`](config/constitution.md), which
every session reads. [`SECURITY.md`](SECURITY.md) covers what is stored, what
is sent where, and how text from mail and web pages is kept from acting as
instructions.

## Where your data lives

The checkout is the program, and it is the same for everyone. `~/.herald` is
you: settings, credentials, and the ledger. Nothing in it is sent anywhere
except to the services you connected and to the model CLI you already use.
Delete the folder and Herald knows nothing about you.

## Where to run it

Herald is most useful on a machine that stays on. A laptop asleep at 06:30
sends that digest late, and a machine off for more than about a day comes back
signed out of Claude. An old laptop or a Raspberry Pi works well.
[`docs/hosting.md`](docs/hosting.md) compares the free options.

## Docs

- [`docs/install.md`](docs/install.md): installing, step by step
- [`docs/docker.md`](docs/docker.md): running it in a container
- [`docs/hosting.md`](docs/hosting.md): where to run it
- [`docs/updates.md`](docs/updates.md): following releases, or owning your copy
- [`docs/extensions.md`](docs/extensions.md): adding your own data sources
- [`docs/extending.md`](docs/extending.md): collectors, cycles and surfaces
- [`docs/architecture.md`](docs/architecture.md): how it is built, and why
- [`docs/operations.md`](docs/operations.md): logs, restarts, and what to do when something breaks

MIT licensed.
