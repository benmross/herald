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
    ("constitution.md", "The rules every session reads",
     "Your agent's operating instructions. This is the one that has to be right."),
    ("about.md", "Who you are", "Read for context at the start of a conversation."),
    ("goals.md", "What you are chasing",
     "Everything it surfaces is ranked against this, so vagueness here becomes "
     "noise in your notifications."),
    ("preferences.md", "How you want to be treated", ""),
]

TIERS = """\
Your agent already follows these. They are worth understanding, because the
second one is the one people are surprised by.

**It acts freely when nothing leaves the machine.** Reading anything, searching
anything, writing to its own notes about you, drafting something for you to
look at.

**It acts, then tells you, for reversible things only you see** — putting an
event it found on a calendar it manages, labelling an email, opening a task.
Never silently: every one of these appears in the next digest.

**It asks first, every time, for anything anyone else sees** — sending a
message, posting anything, applying or registering for anything, spending money,
deleting anything permanently. Overnight it does not ask; it queues the question
for the morning.

Being asked to *look* at something is never permission to *change* it.
"""


def status(state: State) -> tuple[str, str]:
    if not (config.IDENTITY / "constitution.md").exists():
        return TODO, "nothing to review yet — do the interview first"
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
                            help=why + "  Edit anything that is wrong — it is "
                                       "your file."))
    if not fields:
        return Prompt(
            title="Nothing to review",
            blurb="The interview has not produced anything yet. You can skip "
                  "this and come back with `herald setup --step rules`.",
            fields=[], immediate=True, action="Continue")
    return Prompt(
        title="Read what it wrote, and what it may do",
        blurb="Two things worth five minutes now.\n\n" + TIERS
              + "\n\nAnd this is what it wrote about you. Correct anything wrong; "
                "a wrong fact here becomes a wrong assumption every morning.",
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
                   detail="Your agent reads the constitution at the start of "
                          "every session, so this took effect immediately.")


STEP = Step(key="rules", title="Read what it wrote, and what it may do",
            summary="Correct the files, and set the boundary",
            status_fn=status, prompt_fn=prompt, apply_fn=apply)
