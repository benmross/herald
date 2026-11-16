<p align="center">
  <img src="docs/brand/banner.jpg" alt="Herald: a second you that never sleeps" width="100%">
</p>

<p align="center">
  A personal agent that runs on your own machine, on the Claude or ChatGPT subscription you already pay for.
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="docs/install.md">Install guide</a> ·
  <a href="#extensions">Extensions</a> ·
  <a href="#docs">Docs</a>
</p>

---

Herald reads your accounts, writes what it learns into a ledger on your disk,
and works for you around the clock. You talk to it on Telegram, from your phone
or your computer, and that is where it tells you things.

- Reads Gmail, Calendar, Tasks, Contacts, GitHub, any `.ics` feed, job postings, and iMessage on a Mac.
- Remembers every conclusion in plain files you own, so a quiet morning costs almost nothing.
- Drafts replies, manages your calendar, browses, fills in forms, and writes its own extensions.
- Sends a digest at 6:30 AM, and a message the hour it finds something closing tomorrow.
- Asks before anything another person sees, anything that costs money, or anything permanent.
- Runs the official `claude` or `codex` CLI, so there is no API key and no per-token bill.

## Quick start

You need a Claude or ChatGPT subscription, a Google account, and a Mac or
Linux machine with 4 GB of RAM. Windows works through WSL2 or Docker.

```bash
curl -fsSL https://raw.githubusercontent.com/benmross/herald/main/install.sh | bash
herald setup --web
```

Setup opens in your browser and takes about half an hour, mostly you writing
about yourself. New to the terminal? [`docs/install.md`](docs/install.md)
walks through every step.

<details>
<summary><b>Docker</b></summary>

```bash
git clone https://github.com/benmross/herald herald-docker && cd herald-docker
docker compose -f docker/compose.yml up -d --build
docker exec -it herald herald setup --web
```

No sudo, one volume, and a healthcheck that covers the bridge, the database,
your sources and your login. See [`docs/docker.md`](docs/docker.md).

</details>

## Using it

Message your bot on Telegram for everything: questions, errands, changes to
how Herald works ("read this feed too", "send the digest at seven"). Each forum
topic is its own conversation, and replying to an old message picks that
thread back up. Install Telegram on your computer too; it is the same
conversation.

| Telegram command | What it does |
|---|---|
| `/new` | forget this conversation; model and thinking level stay |
| `/stop` | cancel the running turn |
| `/private` | read-only and unrecorded until `/private off` |
| `/models`, `/effort` | pick a model and thinking level; the conversation continues on the same provider |
| `/claude`, `/codex` | switch provider; a new conversation starts with a summary of the topic's work |
| `/context` | what this topic is using and how full its memory is (`/debug` for the technical report) |
| `/update` | update Herald and its extensions |
| `/obligations` | what you owe people |

The terminal is for setup and repairs:

| Terminal command | What it does |
|---|---|
| `herald status` | what it has read, what it cost, what is failing |
| `herald doctor` | find what is broken and how to fix it |
| `herald health` | quick pass or fail |
| `herald update` | update Herald and its extensions |
| `herald setup` | re-run any setup step |
| `herald attach` | a conversation in the terminal, if Telegram is down |

## What it does on its own

| Kind of action | What happens |
|---|---|
| **Just does it** | Reading, searching, writing its notes, drafting. |
| **Does it, then tells you** | Reversible things only you see: a calendar event, a mail label, a task. Listed in the next digest. |
| **Asks every time** | Anything another person sees, anything that costs money, anything permanent. |

The full rules are in [`config/constitution.md`](config/constitution.md).
[`SECURITY.md`](SECURITY.md) covers what is stored, where it goes, and how
text from mail and web pages is kept from acting as instructions.

## Extensions

Send your Herald a repository URL and ask it to install it. It shows you what
the extension runs and leaves it off until you say so.

| Extension | What it does |
|---|---|
| [canvas](https://github.com/benmross/herald-canvas) | Canvas LMS: courses, grades, submissions, announcements |
| [umd](https://github.com/benmross/herald-umd) | UMD campus events, filtered against criteria you write |
| [transit](https://github.com/benmross/herald-transit) | DC buses and Metro: live arrivals, alerts, trip planning |
| [shop](https://github.com/benmross/herald-shop) | clothes in your sizes from H&M, Uniqlo, Old Navy and Kohl's |
| [discord](https://github.com/benmross/herald-discord) | messages from your own Discord client, with club channels read for events |
| [ios](https://github.com/benmross/herald-ios) | build and ship iPhone apps by driving your Mac over SSH |

Setup offers these at the end, and `herald ext available` lists them any time.
Find more under the [`herald-extension`](https://github.com/topics/herald-extension)
topic, or write your own with [`docs/extensions.md`](docs/extensions.md).

## Your data

The checkout is the program, identical for everyone. `~/.herald` is you:
settings, credentials and the ledger. It goes only to the services you
connected and the model CLI you already use. Delete it and Herald forgets you.

Run it on something that stays on. An old laptop or a Raspberry Pi works well;
[`docs/hosting.md`](docs/hosting.md) compares free options.

## Docs

| Doc | Covers |
|---|---|
| [install](docs/install.md) | step-by-step setup |
| [docker](docs/docker.md) | running in a container |
| [hosting](docs/hosting.md) | where to run it |
| [updates](docs/updates.md) | following releases, or owning your copy |
| [extensions](docs/extensions.md) | adding your own sources |
| [extending](docs/extending.md) | collectors, cycles and surfaces |
| [architecture](docs/architecture.md) | how it is built, and why |
| [operations](docs/operations.md) | logs, restarts, fixing things |

MIT licensed.
