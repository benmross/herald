---
name: herald-projects
description: Start or build a software project for the user under projects/: scaffolding, private GitHub repositories, handing a session link over, asking them to authorise something, committing as you go, and batching work on a remote device. Use whenever they ask you to build something new, or for any multi-step build.
---

# Building things with them

`projects/` is where their code lives. The brain serves sessions out of the
repository root, so a session started from a phone or terminal can `cd`
into a project and work there with the full ledger still one directory up.

**Match the surface to the work.** A chat thread is good for starting something,
checking on it, and answering a question. It is a bad place to review a diff or
approve forty tool calls. When work gets real, scaffold it, then give them the
session link and let them drive it where there is a proper interface.

Starting a project:

1. `mkdir projects/<name>`, `git init`, and write enough that the repository
   explains itself: a README saying what it is for, a `.gitignore`, and the
   smallest thing that runs.
2. `gh repo create <name> --private --source=. --remote=origin --push`.
   **Private unless they say otherwise.**
3. Tell them what you made and where, with the repository URL and the session
   link, in three lines.

**When you need them to authorise something** (an OAuth consent screen, an API
key, a signup, a payment method), send them the URL and say exactly what you
need back. They can open the link and paste the result into the same thread.
Asking for this is a normal part of the work. What you must not do is create
the account yourself, enter credentials, or accept terms on their behalf. Those
are theirs to click.

Do not push to a repository they did not ask you to create, and do not make
anything public.

**Building an iOS app? Load the `ios-apps` skill first.** It carries the whole
path from an idea to an App Store submission, learned by shipping one: the
project layout that does not silently break, how to test and screenshot
without being able to tap anything, which parts of signing work headlessly and
which do not, and the traps that each cost a full build cycle the first time.

**Commit at every milestone. Do not wait for the end.** A long build is the
case where you are most likely to be interrupted (a timeout, a usage limit, a
dropped device), and an interruption should cost the next session minutes.
One run once wrote an entire iOS app, 74 steps of it, and committed nothing. The
continuation had to rediscover what existed by reading the tree, and until it
committed, every line was one stray `git checkout` from gone. `git log` is the
cheapest handoff to your next self.

**Batch work on a remote device into one script.** A machine reached over the
network is a round trip away, so build, install, launch, screenshot and fetch as
a single script that runs there and returns one artefact, such as a contact
sheet in place of eight PNGs. Fifteen separate `ssh` calls cost far more wall
clock than the work inside them, and a sleeping lid mid-sequence loses the
whole sequence.
