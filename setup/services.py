"""Running Herald in the background, on either platform.

An agent that only runs when someone types a command is a command-line tool. The
whole proposition -- a digest that is waiting when you wake up, a deadline that
reaches you the hour it is found -- depends on something scheduling it, and that
something is different on the two platforms this supports.

**Linux: systemd user units.** The unit files in `systemd/` are templates:
`{{ROOT}}` for the checkout, `{{GOOGLE}}` for the credentials directory, `%h`
for the home directory (systemd's own). They used to hardcode `%h/herald`, which
worked exactly as long as everyone kept Herald in the same place.

**macOS: launchd agents.** Generated here rather than templated, because a plist
is XML and a timer is a different shape: `StartInterval` for a period,
`StartCalendarInterval` for a wall-clock time. macOS also has no `linger`
concept -- a LaunchAgent runs when the user is logged in, which for a laptop is
what people expect.

The awkward truth about a laptop, said in the setup rather than discovered
later: a Mac that is asleep at 06:30 does not run the 06:30 job. launchd fires
it when the machine wakes, so the digest arrives late rather than never.
"""

from __future__ import annotations

import getpass
import os
import pathlib
import platform
import plistlib
import shutil
import subprocess

from herald import config, extensions, google

SYSTEMD_DIR = pathlib.Path.home() / ".config" / "systemd" / "user"
LAUNCHD_DIR = pathlib.Path.home() / "Library" / "LaunchAgents"
LABEL_PREFIX = "com.herald"


def platform_name() -> str:
    return {"Linux": "systemd", "Darwin": "launchd"}.get(platform.system(), "none")


def render(text: str) -> str:
    return (text.replace("{{ROOT}}", str(config.ROOT))
                .replace("{{GOOGLE}}", str(google.credentials_dir())))


def unit_sources() -> list[pathlib.Path]:
    """Every unit template, Herald's own and every enabled extension's."""
    out = sorted((config.ROOT / "systemd").glob("herald-*"))
    for ext in extensions.enabled():
        out += ext.units()
    return out


# --------------------------------------------------------------------------
# Linux

class ForeignInstall(RuntimeError):
    """Installed units belong to a different checkout.

    Unit names are global to the user, so a second Herald installing its jobs
    would silently take over the first one's -- and the first one would keep
    running, from a directory nothing points at any more. Found by rehearsing a
    fresh install on a machine that already had one.
    """


def foreign_units() -> list[tuple[str, str]]:
    """(unit, the ROOT it points at) for installed units belonging elsewhere."""
    out = []
    if not SYSTEMD_DIR.is_dir():
        return out
    ours = str(config.ROOT)
    for live in sorted(SYSTEMD_DIR.glob("herald-*")):
        text = live.read_text()
        for line in text.splitlines():
            if line.startswith(("ExecStart=", "WorkingDirectory=")):
                value = line.split("=", 1)[1].strip()
                # systemd's own specifiers survive in a unit that was installed
                # before these became templates. Expand the one that matters
                # rather than reporting `%h/herald` as a foreign checkout.
                value = value.replace("%h", str(pathlib.Path.home()))
                # ExecStart may be "<python> <script>"; either way the first
                # path is inside whichever checkout installed it.
                root = value.split()[0] if line.startswith("ExecStart=") else value
                if "/herald" in root and not root.startswith(ours):
                    out.append((live.name, root))
                break
    return out


def install_systemd(force: bool = False) -> list[str]:
    if not force and (foreign := foreign_units()):
        raise ForeignInstall(
            "these installed units belong to a different Herald checkout:\n  "
            + "\n  ".join(f"{name} -> {root}" for name, root in foreign[:5])
            + "\n\nUnit names are global to your account, so installing over them "
              "would take over that install's jobs while it kept running from a "
              "directory nothing points at. Stop and remove the other one first, "
              "or pass --force if this checkout is meant to replace it.")
    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for source in unit_sources():
        target = SYSTEMD_DIR / source.name
        text = render(source.read_text())
        if not target.exists() or target.read_text() != text:
            target.write_text(text)
            written.append(source.name)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    return written


#: unit -> what it is for, and whether it is on by default
SYSTEMD_UNITS = [
    ("herald-collect.timer", "ingestion, every 30 minutes", True),
    ("herald-cycle-dawn.timer", "the morning digest", True),
    ("herald-cycle-scout.timer", "opportunities, twice a day", True),
    ("herald-telegram.service", "the Telegram bridge", None),      # if configured
    ("herald-brain.service", "sessions from the Claude app and the web", True),
    ("herald-watchdog.timer", "restarts anything that stops working", True),
]


def enable_systemd(units: list[str]) -> list[tuple[str, bool, str]]:
    out = []
    for unit in units:
        r = subprocess.run(["systemctl", "--user", "enable", "--now", unit],
                           capture_output=True, text=True)
        out.append((unit, r.returncode == 0, (r.stderr or "").strip()[:200]))
    return out


def enable_linger() -> tuple[bool, str]:
    """Keep the units running when nobody is logged in.

    Needs root, which is the one place setup cannot do it for them. Reported
    rather than attempted silently: a wizard that prompts for a sudo password is
    a wizard nobody should trust.
    """
    user = getpass.getuser()
    r = subprocess.run(["loginctl", "show-user", user, "-p", "Linger", "--value"],
                       capture_output=True, text=True)
    if r.stdout.strip() == "yes":
        return True, "already on"
    return False, f"sudo loginctl enable-linger {user}"


# --------------------------------------------------------------------------
# macOS

def _plist(label: str, args: list[str], *, interval: int | None = None,
           calendar: list[dict] | None = None, keepalive: bool = False,
           run_at_load: bool = False) -> dict:
    out = {
        "Label": label,
        "ProgramArguments": args,
        "WorkingDirectory": str(config.ROOT),
        "EnvironmentVariables": {
            "HOME": str(pathlib.Path.home()),
            "PATH": f"{pathlib.Path.home()}/.local/bin:/opt/homebrew/bin:"
                    f"/usr/local/bin:/usr/bin:/bin",
            "PYTHONPATH": str(google.credentials_dir()),
            "PYTHONUNBUFFERED": "1",
            "HERALD_HOME": str(config.HOME),
            "HERALD_ROOT": str(config.ROOT),
        },
        "StandardOutPath": str(config.LOGS / f"{label}.log"),
        "StandardErrorPath": str(config.LOGS / f"{label}.log"),
    }
    if interval:
        out["StartInterval"] = interval
    if calendar:
        out["StartCalendarInterval"] = calendar
    if keepalive:
        out["KeepAlive"] = True
        out["RunAtLoad"] = True
    if run_at_load:
        out["RunAtLoad"] = True
    return out


def launchd_jobs() -> dict[str, dict]:
    herald = str(config.ROOT / "bin" / "herald")
    python = config.python()
    jobs = {
        f"{LABEL_PREFIX}.collect": _plist(
            f"{LABEL_PREFIX}.collect", [herald, "collect"], interval=1800),
        f"{LABEL_PREFIX}.dawn": _plist(
            f"{LABEL_PREFIX}.dawn", [herald, "cycle", "dawn"],
            calendar=[{"Hour": 6, "Minute": 30}]),
        f"{LABEL_PREFIX}.scout": _plist(
            f"{LABEL_PREFIX}.scout", [herald, "cycle", "scout"],
            calendar=[{"Hour": 8, "Minute": 15}, {"Hour": 16, "Minute": 45}]),
        f"{LABEL_PREFIX}.telegram": _plist(
            f"{LABEL_PREFIX}.telegram",
            [python, str(config.ROOT / "bin" / "herald-telegram")], keepalive=True),
        # The brain lives in a tmux session; `ensure` starts it if it is not
        # there and restarts it if claude has died inside it. Run at load and
        # every five minutes, it is the brain's launcher and its watchdog in
        # one -- the systemd watchdog's other probes are systemctl calls that
        # mean nothing here, so it is not shipped on a Mac.
        f"{LABEL_PREFIX}.brain": _plist(
            f"{LABEL_PREFIX}.brain",
            [str(config.ROOT / "bin" / "herald-brain"), "ensure"],
            interval=300, run_at_load=True),
    }
    return jobs


def install_launchd(labels: list[str] | None = None) -> list[str]:
    LAUNCHD_DIR.mkdir(parents=True, exist_ok=True)
    config.LOGS.mkdir(parents=True, exist_ok=True)
    written = []
    for label, job in launchd_jobs().items():
        if labels is not None and label not in labels:
            continue
        target = LAUNCHD_DIR / f"{label}.plist"
        data = plistlib.dumps(job)
        if not target.exists() or target.read_bytes() != data:
            target.write_bytes(data)
            written.append(label)
        subprocess.run(["launchctl", "unload", str(target)],
                       capture_output=True, check=False)
        subprocess.run(["launchctl", "load", str(target)],
                       capture_output=True, check=False)
    return written


# --------------------------------------------------------------------------

def install(labels: list[str] | None = None, force: bool = False) -> dict:
    """Install (and reload) whatever this platform uses. Does not enable."""
    kind = platform_name()
    if kind == "systemd":
        return {"platform": kind, "written": install_systemd(force=force)}
    if kind == "launchd":
        return {"platform": kind, "written": install_launchd(labels)}
    return {"platform": "none", "written": [],
            "note": "no supported scheduler on this platform; run `herald "
                    "collect` and `herald cycle dawn` by hand, or from cron."}


def wanted_units(telegram_configured: bool) -> list[str]:
    if platform_name() == "systemd":
        return [u for u, _, default in SYSTEMD_UNITS
                if default or (default is None and telegram_configured)]
    if platform_name() == "launchd":
        return [label for label in launchd_jobs()
                if telegram_configured or not label.endswith(".telegram")]
    return []


def status() -> list[tuple[str, str]]:
    """(unit, state) for whatever is installed."""
    out = []
    if platform_name() == "systemd":
        for unit, _, _ in SYSTEMD_UNITS:
            r = subprocess.run(["systemctl", "--user", "is-active", unit],
                               capture_output=True, text=True)
            out.append((unit, r.stdout.strip() or "unknown"))
    elif platform_name() == "launchd":
        listing = subprocess.run(["launchctl", "list"], capture_output=True,
                                 text=True).stdout
        for label in launchd_jobs():
            out.append((label, "loaded" if label in listing else "not loaded"))
    return out


def restart(unit: str) -> tuple[bool, str]:
    """Restart one background job on whichever scheduler this platform has.

    `unit` is the systemd name (`herald-telegram.service`); on macOS it is
    mapped to the launchd label. Both `herald restart` and an update's
    post-restart go through here, which is what stops either from being a
    bare `systemctl` that fails on a Mac with "command not found".
    """
    kind = platform_name()
    if kind == "systemd":
        r = subprocess.run(["systemctl", "--user", "restart", unit],
                           capture_output=True, text=True)
        return r.returncode == 0, (r.stderr or "").strip()[:200]
    if kind == "launchd":
        label = f"{LABEL_PREFIX}." + unit.removeprefix("herald-").removesuffix(".service")
        if label == f"{LABEL_PREFIX}.brain":
            # A oneshot `ensure`; a restart of the brain means restarting the
            # tmux session it supervises, which the script itself does.
            r = subprocess.run([str(config.ROOT / "bin" / "herald-brain"), "restart"],
                               capture_output=True, text=True)
            return r.returncode == 0, (r.stderr or "").strip()[:200]
        target = f"gui/{os.getuid()}/{label}"
        r = subprocess.run(["launchctl", "kickstart", "-k", target],
                           capture_output=True, text=True)
        return r.returncode == 0, (r.stderr or "").strip()[:200]
    return False, "no scheduler on this platform"


def available() -> bool:
    return platform_name() != "none" and bool(shutil.which(
        "systemctl" if platform_name() == "systemd" else "launchctl"))
