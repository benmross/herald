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

There are two engines and no fallback between them. When `claude` or `codex`
hits a usage limit or hangs, the run fails with that error and the user is
told; the other engine does not take over. See "Two engines, chosen, never
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
remembered. Check whether a cycle is escalating unnecessarily, and
whether its snapshot is carrying standing state instead of diffs.

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

## Backups

There are two repositories. The program pushes to the public `herald` repo; the person
(`~/.herald`: config, secrets, ledger, extensions) pushes to the private
`herald-ledger`. What is *not* in either, by design: `facts.db`,
`ledger/raw/`, `ledger/documents/`, `secrets.json`, `projects/*/`.
`ledger/identity/private/` *is* tracked, in the private repo only.

`facts.db` is fully rebuildable from its sources; the documents are not, and
rely on the box's own backups.
