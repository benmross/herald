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


_auth_cache: dict = {}


def claude_auth() -> dict:
    """What `claude auth status` says: {loggedIn, subscriptionType, ...}.

    Asked of the CLI rather than read from ~/.claude/.credentials.json,
    because that file only exists on Linux. On macOS the login lives in the
    Keychain, and a file check there reports every signed-in Mac as signed out.

    Cached for a minute: the answer costs a subprocess, and the wizard asks
    on every page load.
    """
    import time  # noqa: PLC0415
    from herald import config  # noqa: PLC0415
    if _auth_cache and time.monotonic() - _auth_cache["at"] < 60:
        return dict(_auth_cache["value"])
    if not shutil.which("claude"):
        return {}
    try:
        r = subprocess.run(["claude", "auth", "status"], capture_output=True,
                           text=True, timeout=30, env=config.agent_env())
    except (OSError, subprocess.TimeoutExpired):
        return {}
    try:
        import json
        value = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        value = {}
    if value.get("loggedIn"):
        _auth_cache.update(at=time.monotonic(), value=value)
    return dict(value)


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


def _systemd_state() -> tuple[bool, str, str]:
    """Whether systemd is actually running, not merely installed.

    WSL2 is the case that matters: systemctl is on the PATH, and unless
    `[boot] systemd=true` is in /etc/wsl.conf every call to it fails with
    "System has not been booted with systemd". The install step would then
    report that unit by unit, forty minutes in.
    """
    if not shutil.which("systemctl"):
        return False, "not found", ("without it, Herald works when you run it "
                                    "yourself, but nothing happens on a schedule.")
    try:
        r = subprocess.run(["systemctl", "--user", "is-system-running"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)[:80], "systemctl did not answer"
    text = (r.stdout + r.stderr).strip()
    if "not been booted with systemd" in text or "Failed to connect" in text:
        wsl = pathlib.Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists() \
            or "microsoft" in platform.release().lower()
        fix = ("systemd is installed but switched off. "
               + ("On WSL: add these two lines to the file /etc/wsl.conf, "
                  "then run `wsl --shutdown` in Windows and open the terminal "
                  "again:\n\n    [boot]\n    systemd=true" if wsl else
                  "Herald works when you run it yourself; nothing happens on "
                  "a schedule."))
        return False, "installed but not running", fix
    # "running" or "degraded" both mean the user manager is up; degraded only
    # says some unrelated unit failed.
    return True, "running", ""


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
        "name": "Herald's Python environment",
        "ok": venv.exists(), "required": True,
        "detail": "ready" if venv.exists() else "not created yet",
        "fix": f"python3 -m venv {root}/venv && {root}/venv/bin/pip install "
               f"-r {root}/requirements.txt",
    })

    out.append({
        "name": "git", "ok": bool(shutil.which("git")), "required": True,
        "detail": "installed" if shutil.which("git") else "not installed",
        "fix": _brew_or_apt("git", "git"),
    })

    claude = shutil.which("claude")
    out.append({
        "name": "Claude Code", "ok": bool(claude), "required": True,
        "detail": "installed" if claude else "not installed",
        "fix": "curl -fsSL https://claude.ai/install.sh | bash",
    })

    auth = claude_auth() if claude else {}
    sub = auth.get("subscriptionType")
    out.append({
        "name": "Claude Code signed in", "ok": bool(auth.get("loggedIn")),
        "required": True,
        "detail": (f"subscription: {sub}" if sub else
                   "signed in" if auth.get("loggedIn") else "not signed in"),
        "fix": "run `claude auth login` in this terminal and sign in with your "
               "Claude account. Herald runs on your own subscription.",
    })
    if auth.get("loggedIn") and auth.get("apiProvider") not in (None, "firstParty"):
        out.append({
            "name": "Claude Code uses the subscription, not an API provider",
            "ok": False, "required": False,
            "detail": f"apiProvider: {auth.get('apiProvider')}",
            "fix": "Claude Code is set up to bill through a cloud provider "
                   "rather than your subscription. Herald would be charged "
                   "there.",
        })

    tmux = shutil.which("tmux")
    out.append({
        "name": "tmux (keeps a conversation open in the background)",
        "ok": bool(tmux), "required": True,
        "detail": "installed" if tmux else "not installed",
        "fix": _brew_or_apt("tmux", "tmux"),
    })

    ram = memory_gb()
    out.append({
        "name": "memory", "ok": ram is None or ram >= 3.5, "required": False,
        "detail": f"{ram:.1f} GB" if ram else "unknown",
        "fix": "Claude Code needs about 4 GB to run reliably. With less, the "
               "morning digest can be cut off part way through.",
    })

    out.append({
        "name": "no API key set in this terminal",
        "ok": "ANTHROPIC_API_KEY" not in os.environ, "required": False,
        "detail": "one is set" if "ANTHROPIC_API_KEY" in os.environ else "none",
        "fix": "Herald ignores it, so nothing here is billed by the token. It "
               "is only worth knowing that other tools in this terminal will be.",
    })

    system = platform.system()
    if system == "Linux":
        ok, detail, fix = _systemd_state()
        out.append({"name": "background scheduling (systemd)", "ok": ok,
                    "required": False, "detail": detail, "fix": fix})
    elif system == "Darwin":
        out.append({"name": "background scheduling", "ok": True,
                    "required": False, "detail": "macOS handles it", "fix": ""})

    gh = shutil.which("gh")
    out.append({
        "name": "GitHub command line tool (optional)", "ok": bool(gh),
        "required": False,
        "detail": "installed" if gh else "not installed",
        "fix": _brew_or_apt("gh", "gh") + "  (only needed to back up your "
               "agent's notes to GitHub, or to read your GitHub activity)",
    })

    free_gb = shutil.disk_usage(pathlib.Path.home()).free / 1e9
    out.append({
        "name": "disk space", "ok": free_gb > 2, "required": False,
        "detail": f"{free_gb:.1f} GB free",
        "fix": "a few hundred MB is enough to start. What it stores grows "
               "slowly.",
    })
    return out


def status(state: State) -> tuple[str, str]:
    results = checks()
    failed = [c for c in results if c["required"] and not c["ok"]]
    if failed:
        return BLOCKED, "; ".join(c["name"] for c in failed)
    warnings = [c for c in results if not c["required"] and not c["ok"]]
    return DONE, (f"ready ({len(warnings)} optional thing(s) missing)" if warnings
                  else "everything Herald needs is here")


def prompt(state: State) -> Prompt:
    results = checks()
    lines = []
    for c in results:
        mark = "Ready:" if c["ok"] else ("**Missing:**" if c["required"] else "Optional:")
        lines.append(f"- {mark} {c['name']}"
                     + (f" ({c['detail']})" if c["detail"] else ""))
        if not c["ok"] and c["fix"]:
            lines.append(f"    - {c['fix']}")
    blocked = [c for c in results if c["required"] and not c["ok"]]
    blurb = ("A quick look at this computer. Nothing on this page changes "
             "anything.\n\n" + "\n".join(lines))
    if blocked:
        blurb += ("\n\n**Sort out the missing items, then check again.** The "
                  "line under each one is the command that fixes it. Copy it "
                  "into a terminal window and press enter.")
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
    warnings = [f"{c['name']}: {c['fix']}" for c in checks()
                if not c["required"] and not c["ok"]]
    return Outcome(ok=True, message="This computer can run Herald.",
                   warnings=warnings)


STEP = Step(key="preflight", title="Before we start",
            summary="Check this computer has what Herald needs",
            status_fn=status, prompt_fn=prompt, apply_fn=apply)
