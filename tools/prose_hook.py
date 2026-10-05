#!/usr/bin/env python3
"""Stop hook, for Claude Code and Codex alike: read a reply before it is sent.

The constitution tells every session how to write. This hook is what makes
that hold on turn forty as well as on turn one. When a session is about to end
its turn, both CLIs hand this file the reply on stdin. `lib/herald/prose.py`
looks for the stock patterns of machine-written prose, and if the reply has
them the hook exits 2 with the list. Both CLIs read exit 2 from a Stop hook as
"not finished": the session gets the list as feedback and writes the reply
again, and the first draft never reaches the user as the answer.

**Once per reply.** The second time a session tries to stop, the CLI sets
`stop_hook_active`, and this hook lets the reply through whatever it contains.
A check that could refuse twice could refuse forever, and a rewrite that still
trips a rule is usually quoting something or has a reason.

**It costs one model round trip, and only when it fires.** Running it is a
short Python start with no imports from the `herald` package. That is the
trade: a clean reply pays nothing, a flagged one pays a round trip.

**It fails open.** A crash here must not stop every session from ever ending a
turn. Any error is logged and the reply goes out as written.

`prose.mode` in the config is `block` (the default), `warn` (log, change
nothing) or `off`. `prose.off` lists rule names to skip. Every flagged reply is
logged to `ledger/raw/prose.log`, with whether the rewrite came back clean, so
`herald prose --stats` can say which patterns keep coming back.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Loaded by path, as tools/guard.py loads policy.py and for the same reason:
# importing the package would pull in config and db at the end of every turn.
# Registered in sys.modules before it executes because it defines dataclasses.
try:
    _spec = importlib.util.spec_from_file_location(
        "herald_prose", ROOT / "lib" / "herald" / "prose.py")
    prose = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = prose
    _spec.loader.exec_module(prose)
except Exception as _exc:  # noqa: BLE001 -- see main(): fail open, but say so
    prose = None
    _PROSE_ERROR = f"{type(_exc).__name__}: {_exc}"

BLOCK, WARN, OFF = "block", "warn", "off"


def _home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("HERALD_HOME", "~/.herald")).expanduser()


def _log(line: str) -> None:
    if os.environ.get("HERALD_GUARD_PROBE"):
        return
    try:
        path = _home() / "ledger" / "raw" / "prose.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {line}\n")
    except OSError:
        pass


def settings() -> dict:
    """The `prose` block: the program's defaults under the user's own."""
    merged: dict = {}
    for path in (ROOT / "config" / "defaults.json", _home() / "config.json",
                 ROOT / "config" / "herald.json"):
        try:
            merged.update(json.loads(path.read_text()).get("prose") or {})
        except (OSError, ValueError, AttributeError):
            continue
    return merged


def _from_transcript(path: str) -> str:
    """The text of the last assistant message in a Claude Code transcript."""
    try:
        lines = pathlib.Path(path).expanduser().read_text().splitlines()
    except OSError:
        return ""
    for raw in reversed(lines[-200:]):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if entry.get("type") != "assistant":
            continue
        content = (entry.get("message") or {}).get("content") or []
        if isinstance(content, str):
            return content
        text = "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
        if text.strip():
            return text
    return ""


def reply_text(event: dict) -> str:
    text = event.get("last_assistant_message")
    if isinstance(text, str) and text.strip():
        return text
    return _from_transcript(event.get("transcript_path") or "")


def feedback(hits) -> str:
    return (
        "Herald's prose check read the reply you were about to send and found "
        "patterns that read as machine-written:\n\n"
        f"{prose.report(hits)}\n\n"
        "Send the whole reply again with those sentences rewritten. Rewrite the "
        "sentence, not the punctuation: each of these usually marks a place "
        "where a plain statement of the fact was skipped, and removing the "
        "marker alone leaves the gap. Change nothing else, and do not mention "
        "this check or that anything was rewritten. A hit inside text you are "
        "quoting verbatim, or on a word that is the literal name of something, "
        "can stay: put quoted text in quotation marks or a code span and the "
        "check will not read it. This fires once per reply.")


def decide(event: dict) -> tuple[bool, str]:
    """(let the turn end, what to tell the session if not)."""
    conf = settings()
    mode = conf.get("mode", BLOCK)
    if mode not in (BLOCK, WARN):
        return True, ""
    text = reply_text(event)
    if not text.strip():
        return True, ""
    hits = prose.lint(text, off=conf.get("off") or ())
    session = event.get("session_id")
    rules = ",".join(sorted({h.rule for h in hits})) or "-"
    if event.get("stop_hook_active"):
        # The rewrite. It goes out whatever it contains; the log says how it did.
        _log(f"REWROTE {'still-flagged' if prose.flagged(hits) else 'clean'} "
             f"session={session} rules={rules}")
        return True, ""
    if not prose.flagged(hits):
        return True, ""
    _log(f"{'FLAG' if mode == BLOCK else 'WARN'} session={session} rules={rules}")
    if mode == WARN:
        return True, ""
    return False, feedback(hits)


def main() -> int:
    if prose is None:
        _log(f"ERROR prose.py unavailable, checking nothing: {_PROSE_ERROR}")
        return 0
    try:
        event = json.loads(sys.stdin.read() or "{}")
        done, reason = decide(event)
    except Exception as exc:  # noqa: BLE001 -- fail open; see the docstring
        _log(f"ERROR {type(exc).__name__}: {exc}")
        return 0
    if done:
        return 0
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
