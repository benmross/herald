# Installing Herald

Written for someone who has not used a terminal before. If you have, the short
version is at the bottom.

Setting up takes about half an hour, and most of that is you writing about
yourself, which is the part that makes the difference between an agent that can
search your calendar and one that can tell you which thing on it matters.

## What you need first

**A computer that is on when you want Herald working.** A Mac or a Linux
machine with 4 GB of RAM or more. A laptop is fine, but scheduled jobs run only while it is awake. If a provider
requires a fresh login after downtime, `herald doctor` gives its sign-in command.
[`hosting.md`](hosting.md) compares the free options, including what to use if
you do not have a spare machine. Windows works through WSL2 only; the section
[On Windows](#on-windows-wsl2) below has the four things to know before you
start.

**A Claude subscription or a ChatGPT subscription with Codex access.**
Choose Claude only, Codex only, or both. Herald runs the selected first-party
CLI on your subscription, within its usage limits, without a model API key.
Neither provider is required when you choose the other.

**A Google account.** Everything else is built on mail and calendar.

## On Windows (WSL2)

Herald runs inside WSL2, which is a Linux computer inside Windows. Install it
from PowerShell with `wsl --install`, restart, and open **Ubuntu** from the
Start menu. Everything in this guide is then typed into that Ubuntu window,
never into PowerShell.

- Stay on the Linux disk. Run the installer from your Linux home folder
  (type `cd ~` first), never from somewhere under `/mnt/c`. A Windows drive
  cannot keep the credentials file private and corrupts the database, so the
  installer refuses it.
- systemd has to be on. Recent Ubuntu installs have it on already. If the
  installer says it is off, put `[boot]` and `systemd=true` on two lines in
  `/etc/wsl.conf` (`sudo nano /etc/wsl.conf`), run `wsl --shutdown` in
  PowerShell, and open Ubuntu again. Without it nothing runs on a schedule.
- WSL stops when its last window closes. Windows shuts the Linux side
  down about a minute after the last Ubuntu window is closed, and Herald with
  it: no digest, no Telegram replies. Either leave an Ubuntu window open, or
  have Windows keep it alive: in Task Scheduler, create a task that runs at
  log on with the program `wsl.exe` and the arguments
  `-d Ubuntu --exec sleep infinity`.
- A sleeping laptop is a stopped Herald. WSL only runs while Windows is
  awake, so a laptop asleep at 06:30 sends that digest when it wakes.

The wizard's page and the Google sign-in both open in your normal Windows
browser. No port forwarding is needed.

If you would rather not manage WSL at all, [`docker.md`](docker.md) runs
Herald in a container that Docker Desktop keeps alive with no window open.

## Step 1: open a terminal

On a Mac: press `⌘ Space`, type `Terminal`, press enter.
On Linux: `Ctrl+Alt+T`, or find Terminal in your applications.
On Windows: open **Ubuntu** from the Start menu (see above).

A window appears with a blinking cursor. That is where the next line goes.

## Step 2: install

Copy this line, paste it into the terminal, and press enter:

```bash
curl -fsSL https://raw.githubusercontent.com/benmross/herald/main/install.sh | bash
```

It will tell you what it is installing before it installs it, and ask before
anything needs your password. If something is missing that it cannot install for
you, it stops and prints the one command that fixes it.

If it says this window cannot find `herald` yet, close the window and open a
new one first.

When it finishes it prints two options. Use the first:

```bash
herald setup --web
```

That prints a link. Open it. Everything from here happens on a page in your
browser.

> **If you are installing on a different computer than the one you are sitting
> at**, a home server for example, the wizard prints an `ssh -N -L` line as well.
> Run that in a second terminal window on your own computer first, then open the
> link. It makes the server's page reachable from your browser without putting
> anything on the network.

If you would rather stay in the terminal, `herald setup` does exactly the same
thing with the same questions.

## Step 3: the wizard

There are twelve screens. Each one checks that what you just did worked before
offering the next, so you find out about a problem on the screen that caused it.

1. Your model providers offers Claude only, Codex only, or both, with no
   provider preselected. If you choose both, pick the default for scheduled
   jobs and new conversations. Claude Remote Control is a separate optional
   surface and starts off.
2. Before we start checks this computer has what Herald needs.
3. Where your Herald lives creates `~/.herald`, the private folder
   holding everything it will know about you. It offers to keep a version
   history, and separately to back that up to a private GitHub repository. The
   second one is off by default; read what it says before saying yes.
4. You, briefly asks your name, your pronouns, your timezone, and what to call
   your agent.
5. Updates, or a program of your own asks whether your copy receives new
   releases (the default, and the reversible choice) or owns its code and stops
   updating. [`updates.md`](updates.md) is the longer version; in short,
   following costs you almost nothing, because everything you would
   want to change lives outside the program.
6. Google is the long one. Google will not let a program read your account
   until you create a project that asks for permission, so the wizard walks you
   through it a click at a time, with the links. Two things people trip on, both
   called out on the page: you must add yourself as a **test user**, and Google
   will say that the app is **unverified**. It is, because you made it four
   minutes ago and nobody else will ever use it.
7. Telegram is optional, and worth it. This is how your agent reaches you
   when you are not at the machine, and how you answer. You make a bot by
   messaging Telegram's own bot; the wizard walks through it.
8. What else should it read? offers GitHub activity, job postings, calendar
   feeds, and on a Mac your Messages database. All optional.
9. Tell it who you are is the hour. Write about yourself: what you are
   trying to do, what a good week looks like, who matters, what you want to be
   interrupted for. There are prompts, and you can ignore them. You can dictate
   instead of typing, and it saves as you go, so you can stop and come back.
   Then it asks you eight to fifteen follow-up questions about what you
   wrote, and finally turns all of it into the files it reads every day.
10. Read what it wrote shows everything it concluded about you, editable. Fix
   anything wrong now; a wrong fact here becomes a wrong assumption every
   morning.
11. Start it running sets up the background jobs, reads everything once,
   and builds a morning brief without spending anything so you can see what it
   has to work with.
12. Done says what happens next, and how to stop it.

## After that

Nothing more is required. Tomorrow at 06:30 you get your first digest.

Four commands to know:

```bash
herald status      # what it has read, what it cost, what is failing
herald setup       # re-run any single step: herald setup --step google
herald attach      # a terminal conversation with it
herald setup --step providers   # change providers or the default
```

**Optional, Linux: let it ask before running things as root.** Herald's sessions
cannot use sudo, so anything needing root (installing a package, a service
drop-in) is otherwise a command it hands you to paste. `herald root setup` sets
up a second Telegram bot (you make it with @BotFather; the setup walks you
through it) and a small root service, after which Herald can send you the exact
command and run it only if you tap yes. It needs your password once, at setup.

**Safety rules.** `herald safety status` lists the rules Herald applies to every
command its sessions run, whichever engine runs them. Each can be `block`,
`warn` or `off`. Two settings to know on day one: `safety.protected_paths`,
for folders whose existing files must never be deleted or overwritten (a photo
archive with one copy), and `safety.surfaces`, e.g. `{"telegram": "herald"}` to
let Telegram sessions run on Herald's rules alone without Claude's own
permission checks on top.

Most people do not expect that you can **ask it to change itself**: "Also read
this feed", "put my classes on a separate calendar", "stop telling me about
recruiting emails", "send the digest at seven". It edits its own source and
commits the change.

## If something goes wrong

`herald doctor` checks the things that break quietly and says what to do about
each. `herald setup --list` shows which steps are done, and any step can be
re-run on its own without redoing the others.

[`docs/operations.md`](operations.md) covers the rest: where the logs are, how to
restart a service, and what the failure modes look like.

## The short version

```bash
git clone https://github.com/benmross/herald ~/herald && cd ~/herald
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
ln -s ~/herald/bin/herald ~/.local/bin/herald
herald setup            # or: herald setup --web
```

Setting up over SSH, with the browser wizard: forward both the wizard's port
and the one Google redirects to at the end of the sign-in, in one command from
the machine you are sitting at, and leave it running until setup is done:

```bash
ssh -N -L 127.0.0.1:8799:127.0.0.1:8799 -L 127.0.0.1:8765:127.0.0.1:8765 you@server
```

Requires Python 3.11+, git, and your chosen CLI signed in on its subscription.
Install Claude with `curl -fsSL https://claude.ai/install.sh | bash`, then
`claude auth login`; install Codex with
`curl -fsSL https://chatgpt.com/codex/install.sh | sh`, then `codex login`.
These are the first-party installers ([Codex CLI documentation](https://learn.chatgpt.com/docs/cli)).
Only optional Claude Remote Control needs tmux.
For unattended installs, `HERALD_ENGINES=claude`, `codex` or `both` explicitly
chooses which CLIs the installer installs. With no choice it defers to setup.
Models and effort use the CLI defaults; set `engines.claude.model` or
`engines.codex.model` if you want an override. `engines.primary` remains a
legacy alias for `engines.claude`. `$HERALD_HOME` overrides
where your data goes. Everything the wizard does is re-runnable per step.
