# Running Herald

## Day to day

```bash
herald status          # engines, spend by label, collector health, ledger
herald doctor          # the things that break silently
herald brain status    # optional Claude Remote Control
herald attach          # terminal conversation on your chosen provider
herald db --schema     # what is in the ledger and how to query it
herald fact <id> ...   # facts whole, every column, in one call
herald session         # a conversation's context size and where its time went
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
does not take over. A Telegram topic does move on a quota limit if
`engines.auto.enabled` is on, unless it was pinned by a command or `/models`. See "Two engines, chosen, never
fallen back to" in `docs/architecture.md` for what that traded away. A
Telegram topic can be moved by hand with `/codex` or `/claude`, and `herald
status` shows which engines are installed.

Regression tests are offline and do not send messages or invoke a model:

```bash
venv/bin/python -m unittest discover -s tests -v
```

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

## When something breaks

**Collector failing.** The user is told after two consecutive failures and again
when it recovers. `herald collect <name>` runs one regardless of cadence and
prints the traceback. A collector reported as *off* is not failing: it needs a
capability nobody connected, and `herald status` says which.

**Telegram silent.** `systemctl --user status herald-telegram`. There are two
causes, and the unit reads `active (running)` for both, so trust the log over the status.

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
