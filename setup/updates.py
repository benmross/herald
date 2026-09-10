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

TRACKING = "Receive updates (recommended)"
FORK = "Let it change its own program, and stop receiving updates"

BLURB = """\
Herald is one program that a number of people run, and it keeps improving. This
decides whether your copy receives those improvements.

**Receive updates.** Your Herald checks once a day. When there is a new
release it asks you first, with a single tap, before installing it. Nothing you
have written or set up is touched. The trade is that the program itself is not
yours to edit: if you ask your agent to change how Herald works, it will explain
why it cannot, and offer what it can do instead.

That trade is smaller than it sounds, because almost everything you will want
to change is not in the program:

- **settings**, which your agent can change for you when you ask
- **new sources, new routines, new skills**, which live alongside your own
  data as additions rather than changes to the program
- **how it talks to you, what it must never get wrong, what it may not do**,
  which is written in your own half of its rules and read at the start of
  every conversation

**Let it change its own program.** Your Herald owns its program and can rewrite
any part of itself when you ask. You will not receive updates again, so every
later improvement would be yours to bring across by hand. Choose this if you
want Herald as a starting point for something of your own.

You can switch later, but only one direction is free: going from receiving
updates to owning the program keeps everything, while going the other way means
giving up whatever you changed.
"""


def status(state: State) -> tuple[str, str]:
    mode = upstream.mode()
    if mode == "maintainer":
        # Upstream itself. Nothing to choose, and offering to turn the source
        # of the releases into a follower of them would be absurd.
        waiting = len(upstream.unreleased())
        return DONE, ("this is the copy other installs follow"
                      + (f", {waiting} change(s) not yet released" if waiting else ""))
    if not state.step("updates").get("chosen"):
        return TODO, "not chosen yet"
    if mode == "tracking":
        release = upstream.current_release() or "no release yet"
        return DONE, f"receives updates (currently {release})"
    return DONE, "owns its program, no updates"


def prompt(state: State) -> Prompt:
    if upstream.mode() == "maintainer":
        return Prompt(
            title="This copy is the source of updates",
            blurb="This Herald is the one other installs follow. It edits "
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
            help="If you are not sure, receive updates. That is the choice "
                 "you can change your mind about later.")],
        action="Save this")


def apply(state: State, answers: dict) -> Outcome:
    choice = answers.get("mode") or TRACKING
    mode = "fork" if choice == FORK else "tracking"

    local = upstream.local_changes()
    if mode == "tracking" and local["commits"]:
        return Outcome(
            ok=False,
            message=f"This copy of the program already has {len(local['commits'])} "
                    f"change(s) of its own.",
            detail="Receiving updates is only possible for a copy that matches "
                   "the original. Either put the program back as it was "
                   "installed, or choose the second option and keep the changes.")

    hook = upstream.set_mode(mode)
    constitution.write()
    state.step("updates")["chosen"] = mode

    if mode == "tracking":
        warnings = []
        if "not installed" in hook:
            warnings.append(f"the safeguard against editing the program could "
                            f"not be put in place: {hook}")
        return Outcome(
            ok=True,
            message="Your Herald will receive updates, and will ask before installing one.",
            detail="Its rules changed with this: asked for something the "
                   "program does not do, it will reach for a setting, an "
                   "addition of its own, or its notes about you, rather than "
                   "editing the program.",
            warnings=warnings)
    return Outcome(
        ok=True,
        message="Your Herald owns its program. It will not receive updates.",
        detail="`herald update --check` still shows what has changed in the "
               "original, which is worth a look before fixing something that "
               "has already been fixed there.")


STEP = Step(key="updates", title="Updates, or a program of your own",
            summary="Whether your Herald receives updates or owns its program",
            status_fn=status, prompt_fn=prompt, apply_fn=apply)
