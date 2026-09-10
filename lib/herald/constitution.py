"""Assembling the rules a session reads.

Herald's operating rules come in two halves. `config/constitution.md` is the
shared one, tracked in the program's repository and true for every install.
`ledger/identity/constitution.md` is the personal one -- who this Herald belongs
to, the facts about them it must never get wrong, how they want to be spoken to,
and whatever rules their own life imposes. Every session has to read both.

Claude Code loads `CLAUDE.md` from the project root, so the obvious approach was
`@ledger/identity/constitution.md` at the bottom of it. **That does not work
here, and the failure is silent**: `ledger` is a symlink into the user's private
home, and Claude Code will not follow a symlink out of the project when
resolving an import. Tested directly on 9 September 2026 with a behavioural
probe (an instruction planted in each file to begin a reply with a particular
word): the instruction in `CLAUDE.md` took effect, the one in an imported real
path took effect, and the one behind the symlink did not -- neither through
`ledger/...` nor through a symlinked file at the project root.

A silently-dropped import here drops medical facts and academic-integrity rules,
so this does not rely on a feature. `CLAUDE.md` is generated: shared text, then
the personal text, concatenated. It is gitignored, `herald constitution` writes
it, and `herald check` fails when it has drifted from its sources -- including
when someone has edited the generated file directly, which is the mistake this
arrangement invites and which is worth catching rather than overwriting.
"""

from __future__ import annotations

from . import config

SHARED = config.ROOT / "config" / "constitution.md"
PERSONAL = config.IDENTITY / "constitution.md"
OUTPUT = config.ROOT / "CLAUDE.md"

HEADER = """<!--
GENERATED FILE -- do not edit this one.

    config/constitution.md               the rules every Herald shares
    ledger/identity/constitution.md      the rules specific to this person

Edit whichever of those a change belongs in, then run `herald constitution`.
`herald check` fails when this file has drifted from them.

It is generated rather than imported because Claude Code will not follow a
symlink out of the project to resolve an `@import`, and `ledger` is one -- see
lib/herald/constitution.py for the test that established that.
-->

"""

MISSING_PERSONAL = """
---

# This Herald

`ledger/identity/constitution.md` does not exist yet, so you do not know whose
agent you are, what you must never get wrong about them, or how they want to be
spoken to.

**Say so.** Do not infer a personality, a name, or a set of preferences from the
ledger and act as though you were told them. Run `herald setup` -- or ask the
person in front of you to -- and write what you learn into that file.
"""


def render() -> str:
    shared = SHARED.read_text().rstrip() if SHARED.exists() else ""
    if PERSONAL.exists() and PERSONAL.read_text().strip():
        personal = PERSONAL.read_text().strip()
        return f"{HEADER}{shared}\n\n---\n\n{personal}\n"
    return f"{HEADER}{shared}\n{MISSING_PERSONAL}"


def current() -> bool:
    """Whether CLAUDE.md matches what its two sources would produce."""
    return OUTPUT.exists() and OUTPUT.read_text() == render()


def write() -> bool:
    """Regenerate. Returns whether anything changed."""
    text = render()
    if OUTPUT.exists() and OUTPUT.read_text() == text:
        return False
    OUTPUT.write_text(text)
    return True
