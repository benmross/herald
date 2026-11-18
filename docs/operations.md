# Running Herald

## Day to day

```bash
herald status          # engines, spend by label, collector health, ledger
herald doctor          # the things that break silently, and how to fix each
herald health          # quick pass/fail, what Docker's healthcheck runs
herald brain status    # optional Claude Remote Control
herald attach          # terminal conversation on your chosen provider
herald db --schema     # what is in the ledger and how to query it
herald fact <id> ...   # facts whole, every column, in one call
herald session         # a conversation's context size and where its time went
herald debug           # read-only session dashboard; see docs/debug.md
```

## Services

| Unit | What it does |
|---|---|
| `herald-brain.service` | Optional `claude remote-control` in tmux, enabled only when selected in setup, with sessions on demand from the Claude app and claude.ai/code. |
| `herald-watchdog.timer` | every 5 min; probes fd pressure and heartbeats as well as liveness |
| `herald-telegram.service` | the Telegram bridge, one session per forum topic |
| `herald-collect.timer` | every 30 min; each collector runs on its own cadence |
| `herald-cycle-dawn.timer` | 06:30, the morning briefing |
| `herald-cycle-scout.timer` | 08:15 and 16:45, opportunities |

Extensions install their own alongside these. On Linux they are systemd *user*
units; on macOS, launchd agents with the same names under `com.herald.*`.

```bash
herald services install     # render the templates and load them
herald services enable      # start the ones this install wants
herald services status
```

`sudo loginctl enable-linger $USER` keeps the Linux units running when nobody is
logged in. Setup says so rather than asking for a password.

```bash
systemctl --user list-timers 'herald*'
journalctl --user -u herald-telegram -f
journalctl --user -u 'herald-cycle@dawn' -n 100
journalctl --user -u herald-watchdog -n 50        # what the probes saw
```

`herald-watchdog` asks whether each daemon is still doing its job, not whether
its process exists. That distinction cost two hours of silence on 8 Sep 2026.
It logs every pass to `logs/watchdog.log`, restarts on a failed probe, and stops
restarting after three failures in an hour rather than flapping.

## Cost

A cycle's engine has no fallback. When `claude` or `codex` hits a usage limit
or hangs, the run fails with that error and the user is told; the other engine
does not take over. Before a Telegram turn, Auto can move an unpinned topic
whose provider is already at quota. A limit during a turn pauses for a choice.
See "Two engines, chosen, never
fallen back to" in `docs/architecture.md` for what that traded away. A
Telegram topic can be moved by hand with `/codex` or `/claude`, and `herald
status` shows which engines are installed.

Regression tests are offline and do not send messages or invoke a model:

```bash
venv/bin/python -m unittest discover -s tests -v
```

Set `telegram.engine_topic_dots=true` to append 🟠 for Claude, 🔵 for Codex,
or 🟣 for Kiro to forum topic names. The bot needs Manage Topics permission.
Engine commands, model selections, automatic routing and resumed conversations
update the suffix. Private sessions leave the ordinary topic's name alone.
General is excluded. The option defaults to off.

Telegram cannot list existing topic names through the Bot API. Herald learns
names from creation and rename events, and from topic roots included in normal
messages. Existing topics acquire dots as their names become known; renaming
an older topic once also makes its current name available. Unknown names are
left alone. Optional name metadata is added to existing topic records without
changing their session pointers. Failed edits retry in the background.

```bash
herald db "select label, count(*) runs, round(sum(cost_usd),2) usd from runs
           where ts >= datetime('now','-7 days') group by label order by usd desc"
```

Figures are Claude Code's client-side estimate, not the bill; Codex reports
tokens but no dollar figure, so its runs count as $0 here. Steady state is
roughly: dawn ~$0.35, two scout passes ~$0.15 each when quiet, plus Telegram
turns, which comes to under a dollar a day.

If it climbs, the cause is almost always something being re-derived rather than
remembered. Check whether its snapshot is carrying standing state instead of diffs.

Both model CLIs load project instructions, skills and hooks before the prompt.
Batch related work into one session to avoid repeating that context.

With `telegram.narration=messages`, each mid-turn reply is sent once. Following
tool and reasoning updates are appended beneath its prose by editing that same
message, avoiding a separate progress notification. The next mid-turn reply
starts another message; the final answer is delivered separately. Long replies
can span multiple messages, with progress attached to the last chunk. Progress
is trimmed to fit Telegram's limit while preserving the reply above it.

## When something breaks

**Collector failing.** The user is told after two consecutive failures and again
when it recovers. `herald collect <name>` runs one regardless of cadence and
prints the traceback. A collector reported as *off* is not failing: it needs a
capability nobody connected, and `herald status` says which.

**Telegram silent.** `systemctl --user status herald-telegram`. There are two
common local causes, and the unit reads `active (running)` for both, so trust the log over the status.

HTTP 429 means flood control; Herald honors Telegram's `retry_after` cooldown
across the bridge's threads and skips typing during that wait. HTTP 5xx and
network failures use bounded backoff. These failures do not trigger the
plain-text formatting fallback. See Telegram's
[response parameters](https://core.telegram.org/bots/api#responseparameters).

Completed replies are saved in `telegram_outbox` before delivery. A worker
retries pending replies and failed terminal progress edits after recovery,
including across bridge restarts, without rerunning the model or its tools.
Chunks stay in order within a topic, and delivered message IDs retain their
original session for reply-to-message continuation. Private conversations are
never queued. Permanent rejections remain marked `failed` for inspection:

```bash
herald db "select id, created_at, method, attempts, state, last_error from telegram_outbox order by id"
```

Successful rows are removed. A crashed delivery is retried once its five-minute
lease expires. Telegram has no idempotency key for sending a message, so a
connection timeout after acceptance can still produce a duplicate on retry.

**Interrupted turns.** Ordinary incoming requests are committed to
`telegram_turns` before polling acknowledges them. Native session IDs and
message associations are saved at initialization, before tools run. On startup,
interrupted turns continue with their provider, model, effort and transcript.
Albums, split pastes and accepted steering messages stay with the request.
Completed answers are queued and requests consumed in one transaction, so
delivery recovery does not rerun the model. Recovery asks the model to inspect
prior effects; it does not replay tool calls or reuse old approvals. External
side effects cannot be made exactly-once by this journal.

After three interrupted attempts, recovery pauses with a notice. The next user
message continues the saved session, bypassing automatic context renewal once.
`/stop`, `/new`, `/clear` and `/reset` cancel queued recovery. `/resume SESSION_ID`
selects a recorded session from that topic without starting a model turn.
Private sessions are never journaled or recovered. An entering `/private`
fences later messages in the same poll before dispatch.

The systemd bridge has a five-minute main-loop watchdog, a five-second restart
delay, a singleton lock, and a bounded control cgroup. Model/tool workers use
separate transient scopes in `herald-work.slice`, with per-worker limits and
an aggregate budget. Scopes stop with the bridge and are cleaned after each
turn, including daemonized children. `telegram.max_parallel_turns` defaults
to two. `engines.worker_memory_high` and `worker_memory_max` default to 2G and
3G. These scope changes require installed systemd units; macOS and containers
retain their process launching and use the same recovery journal.

For Linux hosts that must preserve remote access, review
`python3 tools/protect-remote-access.py --uid UID`. Its `--apply` operation
requires the separate root approval door. It reserves memory and excludes SSH
and Tailscale from oomd selection, with kernel OOM scores of -1000 and -900.
The user manager is protected at -500. Existing drop-ins are backed up under
`/var/lib/herald-resilience/backups`. It applies resource settings and updates
live daemon scores without restarting either remote-access service. The
bridge remains bounded; model work remains eligible for OOM termination.

A 409 means something else is polling the same bot: Telegram allows exactly one
`getUpdates` consumer. The Claude Code telegram plugin is one candidate: it
polls whatever bot it is configured with and starts with every Claude Code
session (`claude plugin list` to check). If Herald goes quiet the moment you open
a session, that is why.

`Errno 24 Too many open files` means an fd leak. The cause on 8 Sep was
`with db.connect()`, which commits but never closes; see the comment on
`db.session()`. Check with `ls /proc/$(systemctl --user show -p MainPID --value
herald-telegram)/fd | wc -l` and look at what dominates the table: the leaked
handle names the bug. `herald-watchdog` now restarts on this at 85% of the
limit, so it should never again get far enough to go deaf.

**Brain unreachable from the phone.** `herald brain status`. `herald-watchdog`
restarts it within 5 minutes. Remote Control needs a claude.ai login, no
`ANTHROPIC_BASE_URL`, and telemetry not disabled.

**A source says it is stale.** Any collector may write a `health` fact with
`stale` set; the cycle brief then suppresses that source's awaiting-reply list
rather than showing it wrong, because "nobody is waiting" from a sync that
stopped two days ago is worse than saying nothing.

**A session keeps getting blocked.** `ledger/raw/guard.log` has every block and
warning with the session id. `herald safety test <command>` shows which rule a
command trips. A false positive is a bug: fix the rule in `lib/herald/safety.py`
and add the command to the must-allow list in `tests/test_safety.py`. Changing
those files is itself `self_protect`, so the session doing it needs your tap
(`herald safety allow self_protect`).

**"Herald's guard is not working" alert.** `herald-watchdog` probes the guard
every five minutes. Run `herald check`; the usual fix is `herald ext sync`,
which rewrites `.claude/settings.json` and `.codex/hooks.json`. Then find out
what removed it.
A safety rule you lifted with a tap, or set to `warn` or `off`, is not
expected to block and does not raise this.

**Root requests not arriving.** `herald root status`, then `journalctl -u
herald-root -n 50` (system unit, not `--user`). After pulling an update that
touched `bin/herald-rootd`, run `herald root update`; the installed copy does
not change until you do.

**Login expired.** `herald doctor` checks only your enabled providers and gives
their sign-in commands: `claude auth login` or `codex login`. It asks the CLIs
for authentication status and never reads their credential files. A failure
affects that provider; it never switches to the other one.

## Google Tasks changes

Interactive sessions can use `gwrite.task_complete` and `task_reopen` with
`list_name`, Google's `task_id`, and a `user_statement` quoting your explicit
instruction. `tasklist_create` takes `title` and `user_request` quoting your
request for a new list. All take the existing database connection and `actor`.
They record an action and immediately update task facts from Google's response.
Assigned tasks that may affect Docs or Chat collaborators are refused.

These functions refuse a background cycle identified by `HERALD_ACTOR`.
They are not exposed through `herald amber`. Cycles must never infer completion
from deadlines, silence or other activity, or create lists on their own. The
quoted statement is an audit requirement, not proof of identity: a session
must use the user's actual words. Private sessions refuse all three writes.

## GitHub issues

Prepare an issue in a file and preview it without posting:

```bash
herald act issue-open --repo owner/repo --title 'Describe the failure' \
  --body-file /path/to/issue.md --show
```

Run the same command without `--show` to request an approval tap. It displays
the repository, exact title and whole body as literal text, then posts the
stored payload through `red.py` only after approval. Changing the body file
after requesting approval does not change the stored request. A failure
consumes the approval rather than allowing a retry with an old tap.

The whole approval must fit one Telegram message. Longer issues are refused
before a request is sent; shorten the body rather than approving a truncated
preview. This command currently targets GitHub.com and uses your local `gh`
login. Adding the command does not publish anything by itself.

## Five-hour window scheduling

`lib/herald/anchors.py` is on by default on every install. It reads `runs` for
when you usually start, plans when each provider's five-hour window should open,
and sends one word to a cheap model when a window is idle and one is due. The
result is that the windows covering your usual start are nearly empty, and two
providers reset about two and a half hours apart. Five windows would take 25
hours, so each provider runs four a day and has a four-hour gap, which the plan
places where history shows nothing runs. Anything that runs in the gap opens a
window off schedule; the schedule recovers the next day.

It waits for seven days of history and replans weekly. It stops after seven days
without use and at 90% of a weekly quota. `herald usage` prints the plan. Turn
it off with `herald config set usage.anchors.enabled false`. It costs about four
calls a day per provider, recorded in `runs` as `usage:anchor`: about 2,100 tokens
on Claude and 14,000 mostly cached on Codex, measured 30 October 2026.

## Subscription usage

### Multiple Claude subscriptions

If you have added multiple subscription logins with
[claude-swap](https://github.com/realiti4/claude-swap), enable account selection:

```bash
herald config set engines.claude.accounts.enabled true
herald config set engines.claude.accounts.switch_threshold 90
herald config set engines.claude.accounts.switch_margin 20
herald config set engines.claude.accounts.cooldown_seconds 1800
herald usage claude
```

Herald uses `cswap list --json` for usage and `cswap run ACCOUNT
--share-history -- ...` to launch the first-party Claude CLI. It never switches
the machine's default login or runs cswap's global auto-switcher. Leave that
auto-switcher off if you want ongoing default-login sessions to keep their
account. Use the standard `claude` command on PATH with this feature.

A new conversation chooses the available subscription with the lowest
five-hour usage, then its maximum applicable quota usage. Every applicable
window must have capacity. If the model is unspecified, all reported
model-specific windows count. Disabled, expired, stale and unreadable accounts
are excluded. A missing reading never means zero usage. cswap manages its own
usage polling cache; Herald also rejects readings older than 15 minutes.

Once Claude reports a native session ID, Herald saves its account ownership
before checkpointing that ID. Replies, retries, recovery after interruption and
replies to old messages normally retain that account, preserving the chance of
a cache hit. At a turn boundary, Herald may move the session if the account's
maximum applicable usage reaches `switch_threshold` and another subscription
is at least `switch_margin` percentage points lower. Each session has its own
`cooldown_seconds` between moves. The move is saved atomically and announced;
it retains the native transcript and may rebuild its first-request cache.
Usage inspection and quota-choice cards never move an account. Existing
conversations without a binding adopt the default login before evaluating a
move. Do not globally switch that login before those conversations have been
bound. A missing bound account pauses the conversation. Bindings, moves and a
usage cache live in the new additive
`$HERALD_HOME/ledger/raw/claude-accounts.sqlite3` store; no existing data moves.

`/usage` and `herald usage claude` show each account. `--cached` makes no cswap
call. Quota-limit choices use the conversation's account to find its reset;
provider Auto does not move a Claude conversation based on the default login's
unrelated quota reading. A real quota failure still offers the existing
provider-switch, wait or stop choices. An account never changes inside a turn.

This covers Telegram and non-lean `think()` calls, including scheduled cycles.
Private conversations, lean quota-anchor probes, terminal attach and Remote
Control retain their existing default-login behavior. Scheduled quota reminders
and anchor plans still measure that default login. Tokens remain with cswap
and Claude Code; Herald does not read credentials or call a model API.

A Telegram turn stopped by quota pauses with a card showing the current
request, latest progress when available, reset time and remaining quota on the
other enabled engine. Its buttons offer switching to that engine's configured
default model and effort, continuing on the same engine after reset, or stopping.
The switch appears only after a successful fresh quota reading confirms remaining
capacity in every reported window. The tap checks capacity again. A failed,
missing, stale or exhausted reading hides the switch.

A card nobody answers by the reset counts as "continue when limit resets":
the same session continues on the same engine, through the same queued
continuation and attempt cap as the button, and the card says so. It never
switches or stops on its own. Waiting queues one continuation,
90 seconds after the last exhausted window resets. Without a verified reset it
queues a retry in 15 minutes; another limit presents a new choice. A new message
or `/stop` cancels the pending continuation and invalidates the old card. Choices
and queued continuations survive bridge restarts. Switching carries saved work
and the previous transcript into a new native session. Approvals never carry
over. Private sessions retain their existing read-only failure behavior.

If a topic uses a model other than its provider's configured default, the card
also offers continuing on that default after reset. This queues the model and
effort shown on the card without starting inference. At the reset it starts a
fresh native session on the same provider, with the saved work and previous
transcript carried over. The ordinary wait button and unanswered cards still
resume the original model. `/stop` and newer messages cancel either choice.

The optional `limit_choice` topic field is additive; existing saved topic cursors
and `limit_resume` records keep their meaning and need no conversion. Already
queued continuations still run, including ones queued before this change.

For a session stopped before this setting was loaded, local maintenance code
can call `limitresume.request(topic, saved_topic_state, at)` to queue the same
continuation. It writes a one-shot request under `logs/limit-resume-requests/`;
the bridge imports it within 30 seconds. A changed session, provider or last
turn makes it stale. Do not edit the saved bridge cursor to queue work: the
running bridge would overwrite that edit at its next checkpoint.

For a paused task that should resume on a different model of the same provider,
use `limitresume.request(topic, saved_topic_state, at, target={"model": "model-id",
"effort": "high", "fresh": True})`. These requests use the separate
`logs/limit-model-resume-requests/` inbox so an older bridge cannot silently
consume them while ignoring the model. Import checks the original session,
provider, last turn, model, effort and paused record. It can retarget an existing
queue without adding another attempt; a cancelled or changed task wins.
The optional target on a queued continuation is additive. Existing requests and
continuations need no conversion and retain their previous behavior.

`herald usage` and Telegram's `/usage` show subscription percentages and reset
times for your enabled providers, without a model turn. Add `claude` or `codex`
to select one provider. `herald usage --refresh` requests fresh readings;
`herald usage --cached` reads only the saved data. Ordinary requests reuse a
successful reading for five minutes. A private Telegram topic only reads the
cache.

Enable scheduled checks with `herald config set usage.enabled true`. The usage
collector uses the existing collector timer, normally every 30 minutes. No new
service is needed. Other installs keep this off until they enable it.

Claude requires `tmux` and an existing subscription login in the CLI. Each check
opens an isolated tmux server, waits for the normal input prompt, runs the
built-in `/usage` panel through its `/cost` alias, captures its screen, and
closes the server. The alias avoids skills overriding the `/usage` command;
model tools are disabled for this check. Startup trust and login dialogs are never
accepted automatically. Codex uses `account/rateLimits/read` through its
first-party app-server. Herald reads neither provider's credential files.

Reminders fire at 80% and 95% by default. Upcoming deadlines and dated open
obligations lower the threshold to 60% when their preparation time overlaps the
quota window. The defaults consider the next 24 hours and allow two hours of
preparation. Configure these with `usage.thresholds`, `usage.work_threshold`,
`usage.work_horizon_hours`, and `usage.work_lead_hours`. A calendar deadline is
described as a calendar date, since it does not prove an assignment is still
unfinished. Alerts are deduplicated per provider, quota window, reset and
threshold. A check cannot warn about a sudden increase between samples.

An unknown Claude screen or missing field is an error, never a zero-percent
reading. The first failure is reported immediately; the last successful reading
is retained and labelled stale alongside the error. Claude's own last-known
usage screen is also rejected. `herald usage` exits nonzero on an error, an
expired window or data older than `usage.max_age_minutes` (45 by default).
To investigate a changed screen, open the CLI yourself and run `/usage`, then
update `parse_claude` in `lib/herald/usage.py` and the captured-layout tests in
`tests/test_usage.py`. Do not replace an unreadable percentage with zero or
derive subscription percentages from token or dollar totals.

## Credentials

Never printed, quoted, committed or transmitted:

- the Google credentials directory (`google.credentials_dir`, by default
  `~/.config/google-agent`): the OAuth client and token, shared with the
  `google-workspace` skill
- `~/.claude/.credentials.json`: the Claude Code login
- `~/.codex/auth.json`: the Codex login
- `$HERALD_HOME/secrets.json`: every token and private feed URL. Mode 600,
  gitignored even inside the private ledger repo.

`ledger/identity/private/` *is* tracked in the ledger's own repository, which is
why `herald check` verifies that repository is private whenever it has a remote
at all.

### Taking a secret from the user's phone

`herald secret request <path> --why '...'` sends the user a one-time link. The
page behind it (`bin/herald-secrets`, unit `herald-secrets.service`) writes what
they paste into `secrets.json` at `<path>`, logs a green `secret.store` action,
and tells them on Telegram that the path is set. The value never reaches a
session: no route returns it and the spool under `$HERALD_HOME/run/secrets/`
holds only the path, the reason and a hash of the token. A link works once and
expires after 15 minutes (`--minutes`). `herald secret status` lists recent
requests; `herald secret wait <id>` blocks until one is stored.

The server binds to `127.0.0.1:8792` (`surfaces.secret_intake.port`) and stays
off until `surfaces.secret_intake.url` says where the phone reaches it. On a
tailnet:

```bash
tailscale serve --bg --https=8448 http://127.0.0.1:8792
herald config set surfaces.secret_intake.url https://<machine>.<tailnet>.ts.net:8448
herald services enable
```

Do not use `tailscale funnel` or anything else that puts it on the public
internet. The token is long enough that guessing is not the risk; the risk is a
page that writes credentials being reachable by anyone who is sent the link.

## The browser

`herald browse` (skill `herald-browse`, module `lib/herald/browse.py`) needs
nothing set up: the first command starts a headless Chromium with a persistent
profile in `$HERALD_HOME/browser/profile`, DevTools on `127.0.0.1:9333`
(`browser.cdp_port`). The profile holds cookies, so it is gitignored in
`$HERALD_HOME` (migration 0004) and listed under `safety.secret_paths`.

Signing in to a site needs the user's hands, and that is what the service is
for. `bin/herald-browser` (unit `herald-browser.service`) keeps the profile
browser running headed inside Xvfb, which sign-in pages accept more readily,
and serves the page a `herald browse login` link opens: a live picture of one
tab whose taps and typing go straight to it. Nothing typed there is logged or
reaches a session. The link expires (30 minutes by default) or when they tap
Done. It binds to `127.0.0.1:8793` (`surfaces.browser_view.port`) and stays
off until `surfaces.browser_view.url` is set:

```bash
sudo apt install xvfb        # optional; without it the browser runs headless
tailscale serve --bg --https=8449 http://127.0.0.1:8793
herald config set surfaces.browser_view.url https://<machine>.<tailnet>.ts.net:8449
herald services install && herald services enable
```

With `x11vnc` and `novnc` installed (`sudo apt install x11vnc novnc`), a
login link opens a live remote screen of the browser instead of the polled
screenshot page. The service runs x11vnc on the Xvfb display
(127.0.0.1:5993) and websockify (127.0.0.1:6093), which serves the noVNC page
and admits a connection only with a token `herald browse login` wrote. Mount it
under the same URL:

```bash
tailscale serve --bg --https=8449 --set-path /vnc http://127.0.0.1:6093
```

The display is portrait (`browser.screen`, 820x1180) because the person
signing in is usually on a phone.

As with the secret page, never put it behind `tailscale funnel`: whoever opens
a live link is driving a browser that may be signed in to the user's accounts.

Chromium's sandbox is off (`browser.sandbox: false`) because Ubuntu 23.10 and
later block the unprivileged user namespaces it needs unless an AppArmor
profile allows them. Installing such a profile for the Chromium binary and
setting `browser.sandbox` to true turns it back on.

## Filling on a laptop's browser

A remote workspace fills applications in a dedicated Chrome profile on
another machine, so the person reviews and submits in a native window instead
of the live screen. Configure it under `browser.remote`, keyed by workspace
name (`lib/herald/browser_remote.py` documents the fields):

```bash
herald config set browser.remote '{"macbook": {"ssh": "macbook", "remote_port": 9333, "local_port": 9390}}'
herald apply fill <url> --workspace macbook
```

The first use starts Chrome on that machine over SSH with `open -g` (macOS),
remote debugging bound to its loopback, and a separate profile under
`~/Library/Application Support/Herald Chrome`, then forwards the port with
`ssh -L` and minimizes the window. Tabs open in the background and are never
raised, so filling does not take focus. A remote workspace is never chosen
automatically, never counts toward `max_workspaces` and sends no review group:
the forms wait in that browser. Cookies stay on that machine. The machine must
be awake; an unreachable one fails the fill without changing anything.

## Application browser recovery

Managed workers and group review currently require Linux with systemd.
Other platforms retain the original default browser and checkpointed filling.

Application workers are separate `herald-browser-worker@<workspace>.service`
instances. Each owns its profile, display and Chromium process. The review
HTTP controller (`herald-browser-review.service`, localhost 8794 by default)
can restart without killing those browsers. Install and enable it alongside
the original browser service. On a Tailscale Serve installation, add:


```bash
tailscale serve --bg --https=8449 --set-path /groups http://127.0.0.1:8794
```

`herald browse workspaces` lists durable ownership. `herald apply fill` uses
at most `browser.max_workspaces` workers (default two), with at most
`browser.batch_size` applications each (default ten). A verified full group
is sent for review immediately; a shorter final group is handed over at the
end. The review link has an application selector. The person submits through
the employer page and records it with “I submitted this”; Herald never
submits. Finish and close group refuses to discard unsubmitted drafts.
Return for corrections explicitly releases ownership and requires reverification.
Link expiry and controller restarts never release a review reservation.

Checkpoints are append-only in `application_checkpoints` and atomically
written, with fsync, under `$HERALD_HOME/browser/checkpoints`. Attachment
copies are immutable and hash checked. Protected field values are excluded
before a page scan leaves the browser. Authentication remains in protected
profiles; it is not part of application recovery. Review changes are captured
on tab switches and every fifteen seconds while its page is connected.
An unsaved keystroke since the last capture and an expired login may require
the person's hands again. Website draft saving is separate from checkpointing.

Browser identity changes invalidate verified review groups. They require
returning for corrections, recovery and readback verification before a new
review group can be created. `herald apply snapshot` captures open drafts
without navigation. Use `herald apply refill <ids>` for recovery only when
application work is authorized. Identical failures stop after two attempts;
`--retry` is an explicit override after resolving the cause.

`browser.memory_max` configures the original service (default 6G), and
`browser.worker_memory_max` configures each worker (default 3G). Memory
admission checks both machine availability and the worker's own cgroup;
new work pauses at 85 percent of the worker limit. Checkpoint existing drafts
before changing units. `herald services install` reloads units without
restarting the original browser. Never restart it to deploy a review page.

## Backups

There are two repositories. The program pushes to the public `herald` repo; the person
(`~/.herald`: config, secrets, ledger, extensions) pushes to the private
`herald-ledger`. What is *not* in either, by design: `facts.db`,
`ledger/raw/`, `ledger/documents/`, `secrets.json`, `projects/*/`.
`ledger/identity/private/` *is* tracked, in the private repo only.

`facts.db` is fully rebuildable from its sources; the documents are not, and
rely on the box's own backups.

## Updating active Telegram work

Text, photos and documents sent in an active topic are steered into its current
run when the selected transport accepts live input. File captions stay with
their archived paths. Albums wait briefly for all parts, then enter together as
one update. Acknowledgement means the update was queued for the engine; it does
not claim that the model has already read the image or interrupted its current
tool call.

Downloads happen before steering. If the active run finishes during a download,
the file request takes the normal next-turn path without downloading again;
it cannot jump into a replacement run. Private attachments stay in the private
scratch directory. If that private session has ended, Herald asks for a resend
rather than archiving them into an ordinary conversation. Replies that target
another session retain their normal routing, and commands retain their normal
handling. Voice and audio support is unchanged.
