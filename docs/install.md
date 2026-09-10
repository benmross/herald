# Installing Herald

Written for someone who has not used a terminal before. If you have, the short
version is at the bottom.

Setting up takes about half an hour, and most of that is you writing about
yourself, which is the part that makes the difference between an agent that can
search your calendar and one that can tell you which thing on it matters.

## What you need first

**A computer that is on when you want Herald working.** A Mac or a Linux
machine. A laptop is fine — Herald just does less while it is asleep. Windows
works only through WSL2, which is not covered here.

**A Claude subscription**, and the Claude Code app or CLI signed in to it.
Herald works by running Claude Code for you. There is no separate bill, no API
key, and no per-token cost beyond your existing subscription.

**A Google account.** Mail and calendar are the spine of everything else.

## Step 1 — open a terminal

On a Mac: press `⌘ Space`, type `Terminal`, press enter.
On Linux: `Ctrl+Alt+T`, or find Terminal in your applications.

A window appears with a blinking cursor. That is where the next line goes.

## Step 2 — install

Copy this line, paste it into the terminal, and press enter:

```bash
curl -fsSL https://raw.githubusercontent.com/benmross/herald/main/install.sh | bash
```

It will tell you what it is installing before it installs it, and ask before
anything needs your password. If something is missing that it cannot install for
you, it stops and prints the one command that fixes it.

When it finishes it prints two options. Use the first:

```bash
herald setup --web
```

That prints a link. Open it. Everything from here happens on a page in your
browser.

> **If you are installing on a different computer than the one you are sitting
> at** — a home server, say — the wizard prints an `ssh -N -L …` line as well.
> Run that in a second terminal window on your own computer first, then open the
> link. It makes the server's page reachable from your browser without putting
> anything on the network.

If you would rather stay in the terminal, `herald setup` does exactly the same
thing with the same questions.

## Step 3 — the wizard

Ten screens. Each one checks that what you just did actually worked before
offering the next, so you find out about a problem on the screen that caused it.

1. **Before we start** — checks this machine has what Herald needs.
2. **Where your Herald lives** — creates `~/.herald`, the private directory
   holding everything it will know about you. It offers to keep a version
   history, and separately to back that up to a private GitHub repository. The
   second one is off by default; read what it says before saying yes.
3. **You, briefly** — your name, your pronouns, your timezone, and what to call
   your agent.
4. **Google** — the long one. Google will not let a program touch your account
   until you create a project that asks for permission, so the wizard walks you
   through it a click at a time, with the links. Two things people trip on, both
   called out on the page: you must add yourself as a **test user**, and Google
   will warn that the app is **unverified** — it is, because you made it four
   minutes ago and nobody else will ever use it.
5. **Telegram** — optional, and worth it. This is how your agent reaches you
   when you are not at the machine, and how you answer. You make a bot by
   messaging Telegram's own bot; the wizard walks through it.
6. **What else should it read?** — GitHub activity, job postings, calendar
   feeds, and on a Mac your Messages database. All optional.
7. **Tell it who you are** — the hour. Write about yourself: what you are
   trying to do, what a good week looks like, who matters, what you want to be
   interrupted for. There are prompts, and you can ignore them. You can dictate
   instead of typing, and it saves as you go, so you can stop and come back.
   Then it asks you eight to fifteen follow-up questions about what you actually
   wrote, and finally turns all of it into the files it reads every day.
8. **Read what it wrote** — everything it concluded about you, editable. Fix
   anything wrong now; a wrong fact here becomes a wrong assumption every
   morning.
9. **Start it running** — installs the background jobs, reads everything once,
   and builds a morning brief without spending anything so you can see what it
   has to work with.
10. **Done** — what happens next, and how to stop it.

## After that

Nothing more is required. Tomorrow at 06:30 you get your first digest.

Worth knowing:

```bash
herald status      # what it has read, what it cost, what is failing
herald setup       # re-run any single step: herald setup --step google
herald attach      # a terminal conversation with it
herald brain url   # a link that opens the same agent in the Claude app
```

And the thing most people do not expect: **ask it to change itself.** "Also read
this feed", "put my classes on a separate calendar", "stop telling me about
recruiting emails", "send the digest at seven". It edits its own source and
commits the change.

## If something goes wrong

`herald doctor` checks the things that break quietly and says what to do about
each. `herald setup --list` shows which steps are done, and any step can be
re-run on its own without redoing the others.

[`docs/operations.md`](operations.md) covers the rest: where the logs are, how to
restart a service, and what the failure modes actually look like.

## The short version

```bash
git clone https://github.com/benmross/herald ~/herald && cd ~/herald
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
ln -s ~/herald/bin/herald ~/.local/bin/herald
herald setup            # or: herald setup --web
```

Requires Python 3.11+, git, and a signed-in `claude`. `$HERALD_HOME` overrides
where your data goes. Everything the wizard does is re-runnable per step.
