# iMessage

Reads the Messages database on the Mac Herald is running on: who you are
actually talking to, who is waiting on a reply, and what was said.

**macOS only, and only the machine Herald itself runs on.** Pointing at another
Mac over SSH is the obvious next step and is not built yet.

## Turning it on

```bash
herald ext enable imessage
herald config set capabilities.imessage true
herald collect imessage
```

macOS will refuse to let anything read `~/Library/Messages/chat.db` until you
grant **Full Disk Access** to whatever runs Herald — System Settings → Privacy
& Security → Full Disk Access → add your terminal, then restart it. The failure
without it is a bare `unable to open database file`, so `herald collect
imessage` says this in as many words instead.

## What it stores

| Fact | What it is |
|---|---|
| `imessage/thread` | one per conversation: who, when you last spoke, whether the last word was theirs, and whether that counts as waiting on you |
| `imessage/message` | the configured window of messages (30 days by default), for quoting and context |
| `imessage/health` | how fresh the database is |

Nothing older than the window is read, and nothing is ever written back.

## Sending

Off by default, and deliberately awkward to turn on:

```bash
herald config set extensions.imessage.send ask      # red-tier rule
herald config set extensions.imessage.send confirm  # one tap on your phone
```

- **off** — `bin/imessage-send` refuses. Herald drafts; you paste.
- **ask** — it sends when invoked, and the constitution governs when it may be
  invoked: only when you asked in that conversation, never from a cycle. That
  is a rule a session follows, and Herald reads other people's text all day.
- **confirm** — the message goes into the `approvals` table, Telegram shows you
  the exact recipient and text with two buttons, and nothing is sent until you
  tap. The bridge only records the decision; the waiting process is what sends,
  so nothing can approve itself.

Every send is logged to the `actions` table and reported in the next digest,
in every mode. Unattended runs are refused outright.

## Settings

| Key | Meaning |
|---|---|
| `extensions.imessage.send` | `off` (default), `ask`, `confirm` |
| `extensions.imessage.days` | how far back to read, default 30 |
| `extensions.imessage.also_called` | other names people text you by, which is what makes "they addressed you by name" a usable signal |
