"""Step 1: is this machine able to run Herald at all?

Everything here is checked rather than assumed, and every failure names the
exact command that fixes it on this platform. A non-technical person who hits
"python3: command not found" forty minutes into a setup process does not
recover from it; one who is told, on the first screen, to run one line usually
does.

Nothing in this step changes anything. It is allowed to be run over and over.
"""

from __future__ import annotations

import os
import pathlib
import platform
import shutil
import subprocess
import sys

from .engine import BLOCKED, DONE, Field, Outcome, Prompt, State, Step

MIN_PYTHON = (3, 11)


def _brew_or_apt(brew: str, apt: str) -> str:
    if platform.system() == "Darwin":
        return f"brew install {brew}"
    return f"sudo apt install {apt}"


def claude_auth() -> dict:
    """What `claude auth status` says: {loggedIn, subscriptionType, ...}.

    Asked of the CLI rather than read from ~/.claude/.credentials.json,
    because that file only exists on Linux. On macOS the login lives in the
    Keychain, and a file check there reports every signed-in Mac as signed out.
    """
    from herald import config  # noqa: PLC0415
    if not shutil.which("claude"):
        return {}
    try:
        r = subprocess.run(["claude", "auth", "status"], capture_output=True,
                           text=True, timeout=30, env=config.agent_env())
    except (OSError, subprocess.TimeoutExpired):
        return {}
    try:
        import json
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def _claude_logged_in() -> bool:
    return bool(claude_auth().get("loggedIn"))


def _subscription() -> str | None:
    return claude_auth().get("subscriptionType")


def memory_gb() -> float | None:
    """Total RAM, or None where it cannot be read."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
    except (ValueError, OSError, AttributeError):
        pass
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        return int(out) / 1e9 if out else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def checks() -> list[dict]:
    """Every check, as {name, ok, detail, fix, required}."""
    out = []
    ok_py = sys.version_info >= MIN_PYTHON
    out.append({
        "name": f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer",
        "ok": ok_py, "required": True,
        "detail": platform.python_version(),
        "fix": _brew_or_apt("python@3.13", "python3 python3-venv"),
    })

    root = pathlib.Path(__file__).resolve().parents[1]
    venv = root / "venv" / "bin" / "python"
    out.append({
        "name": "Herald's virtualenv",
        "ok": venv.exists(), "required": True,
        "detail": str(venv) if venv.exists() else "not created yet",
        "fix": f"python3 -m venv {root}/venv && {root}/venv/bin/pip install "
               f"-r {root}/requirements.txt",
    })

    out.append({
        "name": "git", "ok": bool(shutil.which("git")), "required": True,
        "detail": shutil.which("git") or "", "fix": _brew_or_apt("git", "git"),
    })

    claude = shutil.which("claude")
    out.append({
        "name": "Claude Code CLI", "ok": bool(claude), "required": True,
        "detail": claude or "not on PATH",
        "fix": "curl -fsSL https://claude.ai/install.sh | bash",
    })

    auth = claude_auth() if claude else {}
    sub = auth.get("subscriptionType")
    out.append({
        "name": "Claude Code signed in", "ok": bool(auth.get("loggedIn")),
        "required": True,
        "detail": (f"subscription: {sub}" if sub else
                   "signed in" if auth.get("loggedIn") else "not signed in"),
        "fix": "run `claude` once and use /login. Herald runs on your own "
               "subscription -- it never uses an API key.",
    })
    if auth.get("loggedIn") and auth.get("apiProvider") not in (None, "firstParty"):
        out.append({
            "name": "Claude Code uses the subscription, not an API provider",
            "ok": False, "required": False,
            "detail": f"apiProvider: {auth.get('apiProvider')}",
            "fix": "Claude Code is configured for a third-party provider "
                   "(Bedrock or Vertex). Herald would then be metered there "
                   "rather than on a subscription.",
        })

    tmux = shutil.which("tmux")
    out.append({
        "name": "tmux", "ok": bool(tmux), "required": True,
        "detail": tmux or "not installed",
        "fix": _brew_or_apt("tmux", "tmux") + "  -- the persistent session "
               "(herald-brain, and `herald attach`) lives in it.",
    })

    ram = memory_gb()
    out.append({
        "name": "memory", "ok": ram is None or ram >= 3.5, "required": False,
        "detail": f"{ram:.1f} GB" if ram else "unknown",
        "fix": "Claude Code wants 4 GB. Below that a cycle can be killed "
               "mid-run; a 1 GB free-tier VPS passes every other check and then "
               "fails at 06:30. See docs/hosting.md.",
    })

    out.append({
        "name": "no ANTHROPIC_API_KEY in the environment",
        "ok": "ANTHROPIC_API_KEY" not in os.environ, "required": False,
        "detail": "set" if "ANTHROPIC_API_KEY" in os.environ else "clear",
        "fix": "Herald strips it before every launch, so this is a warning "
               "rather than a fault -- but a key in your shell means anything "
               "else you run is billed per token.",
    })

    system = platform.system()
    if system == "Linux":
        ok = shutil.which("systemctl") is not None
        out.append({"name": "systemd (for the background services)", "ok": ok,
                    "required": False,
                    "detail": "systemctl found" if ok else "not found",
                    "fix": "without it, Herald still works when you run it by "
                           "hand, but nothing runs on a schedule."})
    elif system == "Darwin":
        out.append({"name": "launchd (for the background services)", "ok": True,
                    "required": False, "detail": "macOS", "fix": ""})

    gh = shutil.which("gh")
    out.append({
        "name": "GitHub CLI (optional)", "ok": bool(gh), "required": False,
        "detail": gh or "not installed",
        "fix": _brew_or_apt("gh", "gh") + "  -- only needed to back your ledger "
               "up to a private repository, or for the GitHub collector.",
    })

    free_gb = shutil.disk_usage(pathlib.Path.home()).free / 1e9
    out.append({
        "name": "disk space", "ok": free_gb > 2, "required": False,
        "detail": f"{free_gb:.1f} GB free",
        "fix": "a few hundred MB is enough to start; the mail and message "
               "history grows slowly.",
    })
    return out


def status(state: State) -> tuple[str, str]:
    failed = [c for c in checks() if c["required"] and not c["ok"]]
    if failed:
        return BLOCKED, "; ".join(c["name"] for c in failed)
    warnings = [c for c in checks() if not c["required"] and not c["ok"]]
    return DONE, (f"{len(warnings)} optional thing(s) missing" if warnings
                  else "everything Herald needs is here")


def prompt(state: State) -> Prompt:
    results = checks()
    lines = []
    for c in results:
        mark = "OK  " if c["ok"] else ("**MISSING**" if c["required"] else "optional")
        lines.append(f"- {mark} — **{c['name']}**"
                     + (f" ({c['detail']})" if c["detail"] else ""))
        if not c["ok"] and c["fix"]:
            lines.append(f"    - {c['fix']}")
    blocked = [c for c in results if c["required"] and not c["ok"]]
    blurb = ("Herald needs a few things on this machine. Nothing here changes "
             "anything — it only looks.\n\n" + "\n".join(lines))
    if blocked:
        blurb += ("\n\n**Fix the missing ones above, then check again.** Each "
                  "line under a missing item is the exact command to run.")
    return Prompt(title="Before we start", blurb=blurb,
                  action="Check again" if blocked else "Looks good",
                  immediate=True, fields=[])


def apply(state: State, answers: dict) -> Outcome:
    failed = [c for c in checks() if c["required"] and not c["ok"]]
    if failed:
        return Outcome(ok=False,
                       message="Still missing: " + ", ".join(c["name"] for c in failed),
                       detail="\n".join(f"{c['name']}: {c['fix']}" for c in failed))
    state.step("preflight")["passed"] = True
    warnings = [f"{c['name']} — {c['fix']}" for c in checks()
                if not c["required"] and not c["ok"]]
    return Outcome(ok=True, message="This machine can run Herald.",
                   warnings=warnings)


STEP = Step(key="preflight", title="Before we start",
            summary="Check this machine has what Herald needs",
            status_fn=status, prompt_fn=prompt, apply_fn=apply)
