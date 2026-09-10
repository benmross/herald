"""Step 8: read what it wrote about you, and decide what it may do.

Two things happen here, and the order matters. First the user reads the files
the interview produced -- an agent's account of them, which they may well want
to change, and which they are far more likely to change now than in six months.
Then they set the boundary: what it may do without asking.

The autonomy tiers are explained with examples rather than named, because
"amber" means nothing to somebody who has not read the architecture and the
distinction it draws is the one that matters most: *reading* something is never
permission to *change* it.
"""

from __future__ import annotations

import pathlib

from herald import config, constitution

from .engine import DONE, TODO, Field, Outcome, Prompt, State, Step

FILES = [
    ("constitution.md", "The rules it follows for you",
     "Your agent's standing instructions, read at the start of every "
     "conversation. This is the one that has to be right."),
    ("about.md", "Who you are", "Read for context at the start of a conversation."),
    ("goals.md", "What you are working towards",
     "Everything it brings to your attention is judged against this, so "
     "anything vague here becomes noise later."),
    ("preferences.md", "How you want to be treated", ""),
]

TIERS = """\
Your agent already follows these rules. They are worth knowing, because the
second one is the one people are surprised by.

**It acts freely when nothing leaves this computer.** Reading, searching,
writing its own notes about you, drafting something for you to look at.

**It acts, then tells you, for things only you see and that can be undone.**
Putting an event it found on a calendar it manages, labelling an email, adding
a task. Never quietly: every one of these appears in your next digest.

**It asks first, every time, for anything anyone else would see.** Sending a
message, posting anything, applying or signing up for anything, spending money,
deleting anything for good. Overnight it does not ask. It saves the question for
the morning.

Being asked to look at something is never permission to change it.
"""


def status(state: State) -> tuple[str, str]:
    if not (config.IDENTITY / "constitution.md").exists():
        return TODO, "nothing to review yet (the interview comes first)"
    if not state.step("rules").get("reviewed"):
        return TODO, "not read through yet"
    return DONE, "reviewed"


def prompt(state: State) -> Prompt:
    fields = []
    for name, label, why in FILES:
        path = config.IDENTITY / name
        if not path.exists():
            continue
        fields.append(Field(key=f"file_{name}", label=label, type="textarea",
                            rows=18, default=path.read_text(),
                            help=why + "  Change anything that is wrong. These "
                                       "are your notes."))
    if not fields:
        return Prompt(
            title="Nothing to review",
            blurb="The interview has not produced anything yet. You can skip "
                  "this and come back to it with `herald setup --step rules`.",
            fields=[], immediate=True, action="Continue")
    return Prompt(
        title="Read what it wrote, and what it may do",
        blurb="Two things worth five minutes now.\n\n" + TIERS
              + "\n\nAnd below is what it wrote about you. Correct anything "
                "wrong. A wrong fact here becomes a wrong assumption every "
                "morning.",
        fields=fields, action="Save and continue")


def apply(state: State, answers: dict) -> Outcome:
    changed = []
    for name, _, _ in FILES:
        key = f"file_{name}"
        if key not in answers:
            continue
        path = config.IDENTITY / name
        new = answers[key]
        if not isinstance(new, str) or not new.strip():
            continue
        if not path.exists() or path.read_text() != new:
            path.write_text(new if new.endswith("\n") else new + "\n")
            changed.append(name)
    state.step("rules")["reviewed"] = True
    if "constitution.md" in changed or not constitution.current():
        constitution.write()
    return Outcome(ok=True,
                   message=("Saved " + ", ".join(changed)) if changed
                           else "Left as written.",
                   detail="Your agent reads these at the start of every "
                          "conversation, so this has taken effect already.")


STEP = Step(key="rules", title="Read what it wrote, and what it may do",
            summary="Correct its notes, and know what it will and will not do",
            status_fn=status, prompt_fn=prompt, apply_fn=apply)
