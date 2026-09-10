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

from . import config, upstream

SHARED = config.ROOT / "config" / "constitution.md"
PERSONAL = config.IDENTITY / "constitution.md"
OUTPUT = config.ROOT / "CLAUDE.md"

HEADER = """<!--
GENERATED FILE -- do not edit this one.

    config/constitution.md               the rules every Herald shares
    lib/herald/constitution.py           the section for this install's mode
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


MODE_SECTIONS = {
    "tracking": """
## This install follows upstream

**The program is not yours to edit.** This Herald tracks tagged releases from
the repository it was installed from, which is what lets it receive fixes and
new features without anybody hand-merging anything. A local commit here makes
that impossible: an update is a fast-forward, and a fast-forward cannot happen
over a change of your own. `git` itself refuses commits in this directory, and
`herald check` reports any drift.

So when the user asks for something the program does not do, **do not edit the
program.** Almost everything they might want is available without touching it:

- **a setting** — `herald config set <path> <value>`, and
  `config/defaults.json` lists what exists
- **a new source, a new cycle, a new skill** — an extension in
  `$HERALD_HOME/extensions/`, which is theirs, is versioned with their ledger,
  and can *override a bundled extension by name*. `herald ext new <name>`, and
  `docs/extensions.md` is the guide. This is the answer far more often than it
  first appears.
- **how it talks to them, what it must never get wrong, what it may not do** —
  `ledger/identity/constitution.md`, which is the half of these rules that
  belongs to them

If what they want genuinely requires changing Herald itself, say so plainly and
offer the two real options: ask upstream for it (the repository's issues), or
`herald mode fork` — which hands them the program and stops updates for good.
Do not make that choice for them.

`herald update` applies a release. It fast-forwards, runs migrations,
regenerates this file, relinks skills, refreshes the background jobs, checks the
invariants, and restarts. If the check fails afterwards it stops before
restarting anything and says so.
""",
    "fork": """
## This install is a fork

The program here is the user's own. It may be edited freely, and **it will not
receive updates** -- that was the trade made when this mode was chosen, and it
is not reversible without discarding local changes.

That makes two habits matter more than they would upstream. **Commit your own
work**, because nobody else's history will ever contain it. And keep the seam
anyway: a change about this person's life still belongs in an extension or in
`ledger/identity/`, not in the program, because that is what keeps it possible
to compare against upstream later or contribute a change back.

`herald update --check` still says what upstream has done, which is worth
reading before solving a problem that has already been solved there.
""",
    "maintainer": """
## This install is upstream

Other people run this program. What is committed and pushed here is what their
copies will eventually receive, and that changes three things about ordinary
work.

**A change to the program is change, commit, push.** Not a question -- the
user develops Herald by asking, and `main` is not what anybody else is running.
Cut a release only when they ask (`herald release`), because a release *is* what
reaches other installs.

**Nothing personal may enter the program.** This is the one condition, and it is
not a matter of care: `herald check` scans every tracked file against this
install's own name, addresses, credentials, account name and home paths, and a
push that has not passed it has no business happening. If a change needs a fact
about this person to work, the fact belongs in `ledger/identity/` or in a
private extension and the program reads it from there. **Run `herald check`
before every push.**

**Somebody else's install has to survive the change.** A new setting is free --
`config/defaults.json` is merged under theirs. A new table or column is nearly
free. Anything that changes the *shape* of what is already stored, or moves a
file somebody's ledger depends on, needs a migration in `migrations/`; see
`lib/herald/migrations.py`. A change that only works on this machine is a change
that breaks everyone else's silently.

Commit messages are the changelog other people read: `herald release` builds it
from them.
""",
}


def render() -> str:
    shared = SHARED.read_text().rstrip() if SHARED.exists() else ""
    mode_section = MODE_SECTIONS.get(upstream.mode(), "").rstrip()
    if mode_section:
        shared = f"{shared}\n\n{mode_section}"
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
