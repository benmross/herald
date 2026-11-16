"""Native Kiro profile generation; model subprocesses live only in think.py."""
import hashlib
import json
from pathlib import Path
import shlex
import re
import os
import tempfile

from . import config

TOOLS = ("read_file", "list_directory", "file_search", "grep_search",
         "fs_write", "fs_append", "str_replace", "delete_file", "execute_bash")


def profile(*, lean=False, read_only=False, no_tools=False, allowed_tools=None):
    """Use a uniquely named global profile so hooks also apply outside ROOT.

    Profile-scoped hooks are inseparable from the selected agent. A workspace
    cannot silently replace the profile: think.py verifies the resolved profile
    before launch and uses a name unique to this install.
    """
    suffix = hashlib.sha256(str(config.ROOT).encode()).hexdigest()[:12]
    tools = [] if lean or no_tools else list(TOOLS[:4] if read_only else TOOLS)
    if allowed_tools is not None:
        tools = [tool for tool in tools if tool in allowed_tools]
    policy = json.dumps([lean, read_only, tools])
    name = "herald-" + suffix + "-" + hashlib.sha256(policy.encode()).hexdigest()[:10]
    value = {"name": name, "description": "Herald native model transport",
             "tools": tools, "includeMcpJson": False, "includePowers": False,
             "mcpServers": {}, "resources": [] if lean else [
                 "file://" + str(config.ROOT / "CLAUDE.md"),
                 "skill://" + str(config.ROOT / ".agents/skills/**/SKILL.md"),
                 *["skill://" + str(Path(path).expanduser() / "**/SKILL.md")
                   for path in config.get("engines.kiro.skill_paths", [])]],
             "prompt": "" if lean else "Follow Herald's instructions. Use its ledger and native tools. "
                       "Do not use specifications, powers, sub-agents or Kiro workflows.",
             "hooks": [{"name": "Herald safety guard", "trigger": "PreToolUse",
                        "action": {"type": "command", "command": shlex.join([
                            config.python(), str(config.ROOT / "tools/kiro_hook.py")])},
                        "timeout": 10}]}
    path = Path.home() / ".kiro/agents" / (name + ".json")
    # Local definitions take precedence over global definitions in Kiro.
    # Refuse a collision rather than launch with an unverified hook.
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, indent=2) + "\n"
    if not path.exists() or path.read_text() != text:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".herald-", suffix=".tmp", delete=False) as stream:
            stream.write(text)
            temporary = stream.name
        os.replace(temporary, path)
    return name


def verify_workspace(name, cwd):
    cwd = Path(cwd).resolve()
    for directory in (cwd, *cwd.parents):
        if directory == Path.home():
            continue
        for extension in (".json", ".md"):
            if (directory / ".kiro/agents" / (name + extension)).exists():
                raise RuntimeError("A workspace overrides Herald's guarded Kiro profile")


def record(result, prompt, label):
    """Keep conversation continuity and native credit receipts, without tools."""
    from . import db
    if not result.session_id or not re.fullmatch(r"[\w-]+", result.session_id):
        return
    try:
        folder = config.RAW / "kiro-sessions"
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / (result.session_id + ".jsonl")).open("a") as stream:
            for role, text in (("user", prompt), ("assistant", result.text)):
                stream.write(json.dumps({"type": role, "timestamp": db.now(),
                    "message": {"content": text}}) + "\n")
        if result.credits_used is not None:
            with db.session() as con:
                db.put_fact(con, "subscription_usage", "consumption", ts=db.now(),
                    data={"engine": "kiro", "session_id": result.session_id,
                          "credits": result.credits_used, "model": result.model, "label": label})
                con.commit()
    except Exception:
        # Bookkeeping cannot lose the answer of a completed model run.
        pass
