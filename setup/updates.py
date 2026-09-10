"""Step: does this install follow upstream, or own its program?

The one question in setup whose answer is hard to change later, and the one
most likely to be answered wrongly by someone being helpful. So it is asked
plainly, with the consequence of each answer stated rather than implied, and
the safe one is the default.

The honest framing, which the screen says out loud: **following upstream costs
almost nothing**, because everything a person actually wants to change lives
outside the program anyway -- settings, their ledger, and extensions that can
override even a bundled one. Forking buys the ability to edit Herald itself and
pays for it by never receiving another fix.
"""

from __future__ import annotations

from herald import config, constitution, upstream

from .engine import DONE, TODO, Field, Outcome, Prompt, State, Step

TRACKING = "Follow updates (recommended)"
FORK = "Let it edit itself, and stop updating"

BLURB = """\
Herald is one program that several people run, and it keeps improving. This
decides whether yours takes those improvements.

**Follow updates.** Your Herald checks once a day, and when there is a new
release it asks you — one tap — before installing it. It fast-forwards, runs any
migrations, and restarts; nothing you have written is touched. The trade is that
the *program* is not yours to edit: your agent will refuse, and git will refuse
too.

That trade is smaller than it sounds, because almost nothing you will want to
change is in the program:

- **settings** — `herald config set …`, and everything that exists is listed in
  `config/defaults.json`
- **new sources, new routines, new skills** — an *extension*, which lives with
  your own data, is yours entirely, and can even replace one Herald ships
- **how it talks to you, what it must never get wrong, what it may not do** —
  your own half of its rules, which it reads at the start of every session

**Let it edit itself.** Your Herald owns its program and can rewrite any part of
itself when you ask. You will not receive updates again — a fast-forward cannot
happen over your own changes — so every later fix would be yours to port by
hand. Choose this if you want Herald as a starting point rather than a program.

You can switch later with `herald mode`, but only one direction is free:
following → fork keeps everything, and fork → following means discarding
whatever you changed.
"""


def status(state: State) -> tuple[str, str]:
    mode = upstream.mode()
    if mode == "maintainer":
        # Upstream itself. Nothing to choose, and offering to turn the source
        # of the releases into a follower of them would be absurd.
        waiting = len(upstream.unreleased())
        return DONE, ("this install is upstream"
                      + (f", {waiting} commit(s) unreleased" if waiting else ""))
    if not state.step("updates").get("chosen"):
        return TODO, f"defaulting to {mode}"
    if mode == "tracking":
        release = upstream.current_release() or "no release tag yet"
        hook = "guarded" if upstream.hook_installed() else "hook missing"
        return DONE, f"following updates ({release}, {hook})"
    return DONE, f"{mode}: self-editing, no updates"


def prompt(state: State) -> Prompt:
    if upstream.mode() == "maintainer":
        return Prompt(
            title="This install is upstream",
            blurb="This Herald *is* the source other installs follow. It edits "
                  "itself, commits and pushes, and `herald release` is what "
                  "reaches everybody else.\n\nThe one condition, which "
                  "`herald check` enforces on every push: nothing personal may "
                  "enter the program. Anything that needs a fact about you "
                  "belongs in your ledger or a private extension, and the "
                  "program reads it from there.",
            fields=[], immediate=True, action="Continue")
    return Prompt(
        title="Updates, or a program of your own",
        blurb=BLURB,
        fields=[Field(
            key="mode", label="Which one?", type="choice",
            choices=[TRACKING, FORK],
            default=TRACKING if upstream.is_tracking() else FORK,
            help="If you are not sure, follow updates. It is the reversible "
                 "one.")],
        action="Save this")


def apply(state: State, answers: dict) -> Outcome:
    choice = answers.get("mode") or TRACKING
    mode = "fork" if choice == FORK else "tracking"

    local = upstream.local_changes()
    if mode == "tracking" and local["commits"]:
        return Outcome(
            ok=False,
            message=f"This checkout already has {len(local['commits'])} commit(s) "
                    f"of its own.",
            detail="Following updates means fast-forwarding, which cannot happen "
                   "over them. Either reset the program directory to what it was "
                   "installed as, or choose the second option and keep them.")

    hook = upstream.set_mode(mode)
    constitution.write()
    state.step("updates")["chosen"] = mode

    if mode == "tracking":
        warnings = []
        if "not installed" in hook:
            warnings.append(f"the commit guard could not be installed: {hook}")
        return Outcome(
            ok=True,
            message="Following updates. Your Herald will ask before installing one.",
            detail="Its own rules changed with this: asked for something the "
                   "program does not do, it will now reach for a setting, an "
                   "extension, or its own memory instead of editing itself.",
            warnings=warnings)
    return Outcome(
        ok=True,
        message="This Herald owns its program. It will not receive updates.",
        detail="`herald update --check` still shows what upstream has changed, "
               "which is worth reading before fixing something twice.")


STEP = Step(key="updates", title="Updates, or a program of your own",
            summary="Whether your Herald follows releases or owns its code",
            status_fn=status, prompt_fn=prompt, apply_fn=apply)
