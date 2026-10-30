"""What was said in a Herald conversation, read from the engine's own transcript.

Only the words of the user and the agent. Tool calls and their results are left
out on purpose: they carry mail, files and command output that belong to their
own sources, and they are where a secret would be. `outcomes` is the one
exception, and it keeps only file paths and the names of commands, never their
arguments. Private sessions are never booked in `runs` and leave no transcript
to find.
"""

from __future__ import annotations

import json
import pathlib
import re

from . import config

_BRIDGE = re.compile(r"\n---\n+Sent [^\n]*\n+")
_SENT = re.compile(r"^Sent \w+day, [^\n]*\n+")
_IMAGE = re.compile(r"^\[Image: original [^\]]*\]\s*", re.M)
_TAGGED = re.compile(r"<(system-reminder|task-notification|environment_context|user_instructions|"
                     r"permissions instructions)[^>]*>.*?</\1>", re.S)
MAX_TURN_CHARS = 4_000


def clean_user(text: str) -> str:
    """The message itself, without what the bridge and the engine wrapped it in."""
    text = _TAGGED.sub("", text or "")
    if text.lstrip().startswith(("Base directory for this skill:", "# AGENTS.md instructions")):
        return ""
    found = list(_BRIDGE.finditer(text))
    if found:
        text = text[found[-1].end():]
    elif text.startswith("This message arrived from the user over"):
        return ""
    return _IMAGE.sub("", _SENT.sub("", text.strip())).strip()


def _claude(path: pathlib.Path):
    for line in path.open(errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") not in ("user", "assistant") or d.get("isSidechain"):
            continue
        content = (d.get("message") or {}).get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        text = "\n".join(c.get("text", "") for c in content or []
                         if isinstance(c, dict) and c.get("type") == "text")
        if text.strip():
            yield d.get("timestamp"), d["type"], text


def _codex(path: pathlib.Path):
    for line in path.open(errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        p = d.get("payload") or {}
        if d.get("type") != "response_item" or p.get("type") != "message":
            continue
        if p.get("role") not in ("user", "assistant"):
            continue
        text = "\n".join(c.get("text", "") for c in p.get("content") or []
                         if isinstance(c, dict) and c.get("text"))
        if text.strip():
            yield d.get("timestamp"), p["role"], text


# Codex wraps a command in a script, so a call can open with a quote.
_ACTION = re.compile(r"(?:^|[;&|\"'\n]\s*)(git (?:commit|push)|gh pr \w+|herald (?:act \S+|deadline add|"
                     r"obligation add|restart|release))\b")
_PATCHED = re.compile(r"\*\*\* (?:Add|Update) File: ([^\n\"'\\]+)")


def outcomes(path: pathlib.Path) -> dict:
    """{files, actions} a session's tool calls touched, in order. Never arguments."""
    files, actions = [], []

    def command(text) -> None:
        actions.extend(m[1] for m in _ACTION.finditer(text if isinstance(text, str) else ""))

    lines = path.read_text(errors="replace").splitlines()
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("isSidechain"):
            continue
        p = d.get("payload") or {}
        if d.get("type") == "response_item" and p.get("type") == "custom_tool_call":
            files.extend(_PATCHED.findall(p.get("input") or ""))
            command(p.get("input"))
        elif d.get("type") == "response_item" and p.get("type") == "function_call":
            try:
                args = json.loads(p.get("arguments") or "{}")
            except ValueError:
                continue
            value = args.get("cmd") or args.get("command") or ""
            command(" ".join(value) if isinstance(value, list) else value)
        elif d.get("type") == "assistant":
            content = (d.get("message") or {}).get("content")
            for c in content if isinstance(content, list) else []:
                if not isinstance(c, dict) or c.get("type") != "tool_use":
                    continue
                given = c.get("input") or {}
                if c.get("name") in ("Write", "Edit") and given.get("file_path"):
                    files.append(given["file_path"])
                elif c.get("name") == "Bash":
                    command(given.get("command"))
    return {"files": list(dict.fromkeys(files)), "actions": list(dict.fromkeys(actions))}


def turns(path: pathlib.Path) -> list[dict]:
    """[{ts, user, reply}] in order. A reply is the agent's text up to the next message."""
    read = _codex if path.name.startswith("rollout-") else _claude
    out: list[dict] = []
    for ts, role, text in read(path):
        if role == "user":
            said = clean_user(text)
            if said:
                out.append({"ts": ts, "user": said[:MAX_TURN_CHARS], "reply": ""})
        elif out:
            # The last text of a turn is its answer; earlier text is narration.
            out[-1]["reply"] = text.strip()[:MAX_TURN_CHARS]
    return out


def locate(session_ids=None) -> dict[str, pathlib.Path]:
    """{session id: transcript}, for sessions run from this checkout."""
    found = {}
    slug = str(config.ROOT).replace("/", "-")
    for path in (pathlib.Path.home() / ".claude" / "projects" / slug).glob("*.jsonl"):
        found[path.stem] = path
    root = pathlib.Path.home() / ".codex" / "sessions"
    if root.is_dir():
        for path in root.rglob("rollout-*.jsonl"):
            found[path.stem[-36:]] = path
    return found if session_ids is None else {k: v for k, v in found.items() if k in session_ids}
