#!/usr/bin/env python3
"""Normalize Kiro V3 tool calls before applying Herald's existing guard.

Unknown tools and malformed inputs fail closed. No parallel agent, connector,
specification or background-process tools are exposed by the Herald profile.
"""
import json
import os
from pathlib import Path
import re
import sys

import guard


def normalize(event):
    name = event["tool_name"]
    args = event["tool_input"]
    if not isinstance(args, dict):
        raise ValueError("tool_input must be an object")
    if name == "disclose_context":
        resource = args.get("name") or args.get("id")
        if isinstance(resource, str):
            resource = resource.removeprefix("skill::")
        if not isinstance(resource, str) or not re.fullmatch(r"[\w:-]+", resource):
            raise ValueError("only registered Herald skills can be loaded")
        sys.path.insert(0, str(guard.ROOT / "lib"))
        from herald import config
        roots = [guard.ROOT / ".agents/skills", *[Path(p).expanduser()
                 for p in config.get("engines.kiro.skill_paths", [])]]
        path = next((p for root in roots for p in root.glob("**/" + resource + "/SKILL.md")
                     if p.is_file()), None)
        if path is None:
            raise ValueError("unknown Herald skill")
        tool, data = "Read", {"file_path": str(path.resolve())}
    elif name == "execute_bash":
        if args.get("run_in_background"):
            raise ValueError("background shell calls are not supported by Herald's Kiro transport")
        command = args.get("command")
        if not isinstance(command, str) or not command:
            raise ValueError("missing shell command")
        tool, data = "Bash", {"command": command}
    elif name in ("read_file", "fs_write", "fs_append", "str_replace", "delete_file"):
        path = args.get("path") or args.get("file_path")
        if not isinstance(path, str) or not path:
            raise ValueError("missing file path")
        tool = "Read" if name == "read_file" else "Edit"
        data = {**args, "file_path": path}
        if name == "delete_file":
            # Deletion needs the same destructive-operation classification as shell.
            import shlex
            tool, data = "Bash", {"command": "rm -- " + shlex.quote(path)}
    elif name in ("list_directory", "file_search", "grep_search"):
        path = args.get("path") or args.get("directory") or event.get("cwd") or os.getcwd()
        tool, data = "Read", {"file_path": path}
    else:
        raise ValueError("unsupported Kiro tool: " + str(name))
    return {**event, "cwd": args.get("cwd") or event.get("cwd") or os.getcwd(),
            "tool_name": tool, "tool_input": data}


def main():
    try:
        if guard.policy is None or guard.safety is None:
            raise RuntimeError("Herald guard could not load")
        event = normalize(json.load(sys.stdin))
        allowed, reason = guard.decide(event)
        if allowed:
            return 0
    except Exception as exc:
        reason = "Blocked Kiro tool: " + str(exc)
    guard._log("BLOCK Kiro :: " + reason[:300])
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
