"""Step 9: start it running, and prove it works.

Up to here nothing has actually happened on a schedule. This step installs the
background jobs, runs every collector once so the ledger is not empty, and
builds a morning brief without spending a token -- which is the cheapest
possible proof that the whole chain works, from credentials through collectors
through the snapshot the digest is written from.

The first real digest is offered rather than run: it costs money and it sends a
notification, and the first thing a new install should do is not surprise
somebody.
"""

from __future__ import annotations

import subprocess
import sys

from herald import capabilities, config

from . import services
from .engine import DONE, PARTIAL, TODO, Field, Outcome, Prompt, State, Step


def status(state: State) -> tuple[str, str]:
    if not services.available():
        return DONE, "no scheduler on this platform — run it by hand"
    running = [name for name, state_ in services.status()
               if state_ in ("active", "loaded")]
    if not running:
        return TODO, "nothing is running yet"
    if not state.step("install").get("collected"):
        return PARTIAL, f"{len(running)} job(s) installed, nothing collected yet"
    return DONE, f"{len(running)} background job(s) running"


def prompt(state: State) -> Prompt:
    kind = services.platform_name()
    telegram = bool(config.secret("telegram.bot_token"))
    lines = []
    if kind == "systemd":
        lines.append("Herald will install systemd user units: ingestion every 30 "
                     "minutes, a digest at 06:30, opportunities twice a day, a "
                     "watchdog every 5 minutes"
                     + (", and the Telegram bridge." if telegram else "."))
        ok, how = services.enable_linger()
        if not ok:
            lines.append(f"\n**One command needs your password**, and setup will "
                         f"not ask for it. After this finishes, run:\n\n    {how}\n\n"
                         f"Without it the jobs stop when you log out.")
    elif kind == "launchd":
        lines.append("Herald will install launchd agents: ingestion every 30 "
                     "minutes, a digest at 06:30, opportunities twice a day"
                     + (", and the Telegram bridge." if telegram else "."))
        lines.append("\nOn a laptop, a job scheduled for 06:30 runs when the "
                     "machine next wakes, so the digest arrives late rather than "
                     "never.")
    else:
        lines.append("This platform has no scheduler Herald knows how to use. "
                     "Everything still works when you run `herald collect` and "
                     "`herald cycle dawn` yourself.")
    lines.append("\nThen it runs every collector once — the first pass reads more "
                 "than later ones and can take a few minutes — and builds a "
                 "morning brief without spending anything, so you can see what it "
                 "would have to work with.")
    return Prompt(title="Start it running", blurb="\n".join(lines),
                  fields=[Field(
                      key="digest", type="bool", default=False,
                      label="Also send a first digest now",
                      help="Costs a few cents on your subscription and sends you "
                           "a notification. Otherwise the first one arrives at "
                           "06:30 tomorrow.")],
                  action="Start it")


def apply(state: State, answers: dict) -> Outcome:
    warnings = []
    telegram = bool(config.secret("telegram.bot_token"))

    try:
        result = services.install()
    except services.ForeignInstall as exc:
        return Outcome(ok=False, message="Another Herald owns the background jobs.",
                       detail=str(exc))
    if result["platform"] == "none":
        warnings.append(result.get("note", ""))
    else:
        units = services.wanted_units(telegram)
        if result["platform"] == "systemd":
            for unit, ok, err in services.enable_systemd(units):
                if not ok:
                    warnings.append(f"{unit}: {err}")
            ok, how = services.enable_linger()
            if not ok:
                warnings.append(f"run `{how}` so the jobs survive logging out")
        state.step("install")["units"] = units

    # Ingest once, so nothing downstream is reasoning about an empty ledger.
    collect = subprocess.run([str(config.ROOT / "bin" / "herald"), "collect", "--force"],
                             capture_output=True, text=True, timeout=1800)
    state.step("install")["collected"] = True
    tail = (collect.stdout or "").strip().splitlines()[-12:]

    # The free proof that the chain works.
    snapshot = subprocess.run(
        [config.python(), str(config.ROOT / "cycles" / "_snapshot.py"), "dawn"],
        capture_output=True, text=True, timeout=300)
    if snapshot.returncode != 0:
        warnings.append("the morning brief did not build: "
                        + (snapshot.stderr or "").strip()[-300:])

    detail = "\n".join(tail)
    if snapshot.returncode == 0:
        lines = len(snapshot.stdout.splitlines())
        detail += f"\n\nBuilt a {lines}-line morning brief from that, for free."

    if answers.get("digest"):
        cycle = subprocess.run([str(config.ROOT / "bin" / "herald"), "cycle", "dawn"],
                               capture_output=True, text=True, timeout=1800)
        if cycle.returncode == 0:
            detail += "\n\n" + (cycle.stdout or "").strip()[-400:]
        else:
            warnings.append("the first digest failed: "
                            + (cycle.stderr or "").strip()[-300:])

    unusable = [k for k in capabilities.registry()
                if capabilities.enabled(k) and not capabilities.available(k)]
    for key in unusable:
        warnings.append(f"{key} is on but not usable: {capabilities.missing(key)}")

    return Outcome(ok=True, message="Running.", detail=detail, warnings=warnings)


STEP = Step(key="install", title="Start it running",
            summary="Install the background jobs and prove the chain works",
            status_fn=status, prompt_fn=prompt, apply_fn=apply)
