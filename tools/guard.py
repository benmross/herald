#!/usr/bin/env python3
"""Claude Code PreToolUse hook: stop a session writing to Google outside the door.

`tools/check.py` holds the program's tracked files to the tier rules. It cannot
see a script a session writes to /tmp and runs a minute later, and that is
exactly how the one unlogged amber action in Herald's history happened: a Drive
upload through a runtime script that never touched gwrite.

This hook sees the tool call before it runs. For Bash it finds whatever is about
to be *executed as Python* -- a file handed to an interpreter or `grun`, a `-c`
string, a heredoc piped into python -- parses it with `policy.find_calls`, and:

    red     blocked. Points at red.py, which puts the exact action on the user's
            phone and acts only on their tap.
    amber   blocked, because it would not be logged. Points at the gwrite
            function that does the same thing and logs it.
    green   allowed.

Connector tools (`mcp__...`) are classified by name the same way.

**Only execution is inspected, never text.** A heredoc into `cat`, a commit
message, a journal entry -- all of those routinely *mention* `messages().send()`,
and this session's own documentation of the risk was the first false positive
the old regex produced. Writing a file is not running it, so Write and Edit are
not hooked at all: the guard, gwrite and the tests all have to name these
methods to exist.

**It fails open, loudly.** A crash here must not brick every Bash call in every
session, which would be far worse than the accident it prevents. An internal
error is logged to `ledger/raw/guard.log` and the call proceeds. Every block is
logged there too, so a false positive can be found and fixed rather than
silently worked around.

It is a tripwire, not a sandbox. A session set on evading it can assemble a
method name at runtime. What stops a *fooled* session is the tap in red.py.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import pathlib
import re
import shlex
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Loaded by path, not through the `herald` package: this runs before every Bash
# call in every session, and importing the package would pull in config, db and
# more for a check that needs only the classifier.
#
# The module has to be registered in sys.modules before it executes. policy.py
# defines a dataclass, and dataclasses look their own module up there; loaded
# by path without registering, it raised at import on Python 3.14. That failure
# is the dangerous kind: an import crash exits 1, not 2, which Claude Code treats
# as a non-blocking hook error, so the tool call proceeds and a guard that is
# installed guards nothing, silently. Caught in tests on 12 Sep 2026, before
# the hook was ever live. `herald check` now runs the guard against a red probe
# for exactly this reason.
try:
    _spec = importlib.util.spec_from_file_location(
        "herald_policy", ROOT / "lib" / "herald" / "policy.py")
    policy = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = policy
    _spec.loader.exec_module(policy)
except Exception as _exc:  # noqa: BLE001 -- see main(): fail open, but say so
    policy = None
    _POLICY_ERROR = f"{type(_exc).__name__}: {_exc}"

# Files that are the door itself. Running them directly is not a bypass.
DOOR = {(ROOT / "lib" / "herald" / "gwrite.py").resolve(),
        (ROOT / "lib" / "herald" / "red.py").resolve()}

GWRITE_FOR = {
    ("events", "insert"): "gwrite.calendar_insert",
    ("events", "update"): "gwrite.calendar_update",
    ("events", "patch"): "gwrite.calendar_patch",
    ("events", "delete"): "gwrite.calendar_delete_own (events Herald created) "
                          "or gwrite.calendar_cancel",
    ("tasks", "insert"): "gwrite.task_create",
    ("messages", "modify"): "gwrite.gmail_label",
    ("messages", "trash"): "gwrite.gmail_trash",
    ("labels", "create"): "gwrite.gmail_label_id(..., create=True)",
    ("files", "create"): "gwrite.drive_create",
    ("files", "update"): "gwrite.drive_update (content or name) or gwrite.drive_trash",
    ("documents", "create"): "gwrite.doc_create",
    ("documents", "batchUpdate"): "gwrite.doc_batch_update",
    ("people", "updateContact"): "gwrite.contact_set_address (addresses only)",
}

INTERPRETER = re.compile(r"(^|/)(python3?(\.\d+)?|grun)$")


def _log(line: str) -> None:
    # `herald check` runs the guard against a synthetic red call to prove it
    # still blocks. Those probes are not blocks worth auditing.
    if os.environ.get("HERALD_GUARD_PROBE"):
        return
    try:
        home = pathlib.Path(os.environ.get("HERALD_HOME", "~/.herald")).expanduser()
        path = home / "ledger" / "raw" / "guard.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {line}\n")
    except OSError:
        pass


def _heredocs(command: str) -> list[tuple[str, str]]:
    """(the command word before `<<TAG`, the heredoc body) pairs."""
    out = []
    for m in re.finditer(r"([^\n|;&]*?)<<-?\s*(['\"]?)(\w+)\2[^\n]*\n", command):
        lead, tag = m.group(1), m.group(3)
        end = re.search(rf"^\s*{re.escape(tag)}\s*$", command[m.end():], re.M)
        body = command[m.end(): m.end() + end.start()] if end else command[m.end():]
        out.append((lead, body))
    return out


def python_sources(command: str, cwd: str) -> list[tuple[str, str]]:
    """Every piece of Python a Bash command is about to execute, labelled."""
    sources = []
    for lead, body in _heredocs(command):
        words = lead.strip().split()
        if any(INTERPRETER.search(w) for w in words):
            sources.append(("heredoc", body))
    # Everything outside heredoc bodies, split into simple commands.
    outside = command
    for _, body in _heredocs(command):
        outside = outside.replace(body, "\n")
    for segment in re.split(r"&&|\|\||[;|\n]", outside):
        try:
            words = shlex.split(segment, comments=False, posix=True)
        except ValueError:
            continue
        for i, w in enumerate(words):
            if not INTERPRETER.search(w):
                continue
            rest = words[i + 1:]
            if "-c" in rest and rest.index("-c") + 1 < len(rest):
                sources.append(("-c", rest[rest.index("-c") + 1]))
                break
            target = next((a for a in rest if not a.startswith("-")), None)
            if target and target != "-":
                path = pathlib.Path(os.path.expanduser(target))
                if not path.is_absolute():
                    path = pathlib.Path(cwd) / path
                if path.is_file() and path.resolve() not in DOOR:
                    try:
                        sources.append((str(path), path.read_text(errors="replace")))
                    except OSError:
                        pass
            break
    return sources


def decide(event: dict) -> tuple[bool, str]:
    """(allowed, reason). Pure: no I/O beyond reading scripts to be run."""
    tool = event.get("tool_name") or ""
    tool_input = event.get("tool_input") or {}

    tier = policy.classify_mcp(tool)
    if tier == policy.RED:
        return False, (
            f"Blocked `{tool}`: it reaches another person, which is red. Red actions "
            f"go through lib/herald/red.py, which shows the user exactly what will "
            f"happen and acts only on their tap -- e.g. `herald act mail-send`.")
    if tier == policy.AMBER:
        return False, (
            f"Blocked `{tool}`: a connector write is not logged to the actions "
            f"table, so the digest could never report it. Do the same thing from a "
            f"script through lib/herald/gwrite.py, which logs before it returns.")
    if tier == policy.GREEN:
        return True, ""

    if tool != "Bash":
        return True, ""

    command = tool_input.get("command") or ""
    cwd = event.get("cwd") or os.getcwd()
    found = []
    for label, src in python_sources(command, cwd):
        for call in policy.writes(src):
            found.append((label, call))
    if not found:
        return True, ""

    worst = policy.worst(c for _, c in found)
    listed = "; ".join(f"{c} in {label}" for label, c in found[:4])
    if worst == policy.RED:
        return False, (
            f"Blocked: this script makes a red Google call ({listed}). Sending, "
            f"sharing and permanent deletion go through lib/herald/red.py, which puts "
            f"the exact action on the user's phone and acts only on their tap. For "
            f"email: `herald act mail-send --to ... --subject ... --body-file ...`. "
            f"If the user asked for something red that red.py has no kind for, say "
            f"so rather than working around this.")
    hints = sorted({GWRITE_FOR.get((c.resource, c.method),
                                   f"(nothing in gwrite for {c.resource}().{c.method}"
                                   f"() yet: add a logged function there)")
                    for _, c in found})
    return False, (
        f"Blocked: this script writes to Google directly ({listed}), so the write "
        f"would never reach the actions table and the digest could not report it. "
        f"Run it with Herald's venv and use the logged equivalent instead: "
        f"{'; '.join(hints)}. Pattern: `./venv/bin/python` with "
        f"`sys.path.insert(0, 'lib')`, `from herald import db, gwrite`, and pass "
        f"`con` from `db.session()`.")


def main() -> int:
    if policy is None:
        _log(f"ERROR policy unavailable, guarding nothing: {_POLICY_ERROR}")
        return 0
    try:
        event = json.loads(sys.stdin.read() or "{}")
        allowed, reason = decide(event)
    except Exception as exc:  # noqa: BLE001 -- fail open; see the docstring
        _log(f"ERROR {type(exc).__name__}: {exc}")
        return 0
    if allowed:
        return 0
    _log(f"BLOCK {event.get('tool_name')} session={event.get('session_id')} :: {reason[:300]}")
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
