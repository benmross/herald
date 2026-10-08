"""Preserve old choices when removing the program's historical provider defaults.

Only upgrades run this. Fresh installs stamp migrations and choose in setup.
Keep legacy config values as well as copying them, so nothing is discarded.
"""
from __future__ import annotations

import json
from herald import config

DESCRIPTION = "name both providers equally while preserving existing install choices"


def apply(con) -> str:
    path = config.CONFIG_PATH
    if not path.exists():
        return "no existing configuration"
    data = json.loads(path.read_text())
    engines = data.setdefault("engines", {})
    # These are the old shipped values, frozen only on upgrades. They are
    # historical compatibility, never defaults for a new installation.
    legacy = {"model": "sonnet", "escalate_model": "opus", **engines.get("primary", {})}
    engines.setdefault("claude", {})
    for key, value in legacy.items():
        engines["claude"].setdefault(key, value)
    if not engines.get("default_engine"):
        enabled = engines.get("enabled")
        engines["default_engine"] = enabled[0] if enabled and len(enabled) == 1 else "claude"
    engines.setdefault("enabled", ["claude", "codex"])
    engines.setdefault("codex", {}).setdefault("escalate_effort", "xhigh")
    # The former primary permission setting applied to both adapters. Freeze
    # that too, especially plan mode, so this change cannot relax a restriction.
    if legacy.get("permission_mode") is not None:
        engines["codex"].setdefault("permission_mode", legacy["permission_mode"])
    data.setdefault("surfaces", {}).setdefault("remote_control", {}).setdefault("enabled", "claude" in engines["enabled"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)
    config.reload()
    # Topics without an engine/model were formerly Claude/Opus. Freeze those
    # choices, so changing new-topic defaults cannot reinterpret transcripts.
    row = con.execute("SELECT cursor FROM collector_state WHERE collector='telegram'").fetchone()
    if row and row[0]:
        state = json.loads(row[0])
        for topic in state.get("threads", {}).values():
            topic["engine"] = topic.get("engine") or "claude"
            if topic["engine"] == "claude" and not topic.get("model"):
                topic["model"] = "opus"
        state["provider_choices_version"] = 1
        con.execute("UPDATE collector_state SET cursor=? WHERE collector='telegram'", (json.dumps(state),))
    con.execute("UPDATE telegram_sessions SET engine='claude', model=COALESCE(model,'opus') WHERE engine IS NULL")
    return "existing provider/model choices preserved; new installs choose explicitly"
