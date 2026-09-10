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
        return DONE, "this computer cannot run things on a schedule; run it by hand"
    running = [name for name, state_ in services.status()
               if state_ in ("active", "loaded")]
    if not running:
        return TODO, "not running yet"
    if not state.step("install").get("collected"):
        return PARTIAL, "running in the background, nothing read yet"
    return DONE, "running in the background"


def prompt(state: State) -> Prompt:
    kind = services.platform_name()
    telegram = bool(config.secret("telegram.bot_token"))
    lines = []
    if kind == "systemd":
        lines.append("Herald will set itself up to run in the background: "
                     "reading what you connected every 30 minutes, a digest at "
                     "06:30, a look for opportunities twice a day, and a check "
                     "every 5 minutes that everything is still working"
                     + (", plus the Telegram connection." if telegram else "."))
        ok, how = services.enable_linger()
        if not ok:
            lines.append(f"\n**One command needs your password**, and setup "
                         f"will not ask for it. After this step finishes, run "
                         f"this in a terminal window:\n\n    {how}\n\n"
                         f"Without it, everything stops when you log out.")
    elif kind == "launchd":
        lines.append("Herald will set itself up to run in the background: "
                     "reading what you connected every 30 minutes, a digest at "
                     "06:30, and a look for opportunities twice a day"
                     + (", plus the Telegram connection." if telegram else "."))
        lines.append("\nOn a laptop, something scheduled for 06:30 runs when "
                     "the laptop next wakes, so the digest arrives late rather "
                     "than not at all.")
    else:
        lines.append("This computer has no way Herald knows of to run things on "
                     "a schedule. Everything still works when you run `herald "
                     "collect` and `herald cycle dawn` yourself.")
    lines.append("\nThen it reads everything you connected once. The first "
                 "pass reads more than later ones and can take a few minutes. "
                 "After that it builds a morning brief, without using any of "
                 "your subscription, so you can see what it has to work with.")
    return Prompt(title="Start it running", blurb="\n".join(lines),
                  fields=[Field(
                      key="digest", type="bool", default=False,
                      label="Also send a first digest now",
                      help="Uses a little of your subscription and sends you a "
                           "message. Otherwise the first one arrives at 06:30 "
                           "tomorrow.")],
                  action="Start it")


def apply(state: State, answers: dict) -> Outcome:
    warnings = []
    telegram = bool(config.secret("telegram.bot_token"))

    try:
        result = services.install()
    except services.ForeignInstall as exc:
        return Outcome(ok=False, message="Another copy of Herald is already "
                                         "running in the background on this "
                                         "computer.",
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
                warnings.append(f"run `{how}` in a terminal so Herald keeps "
                            f"running after you log out")
        state.step("install")["units"] = units

    # A fresh database is created from the current schema, so every migration
    # that exists has, by definition, already happened to it. Recording them as
    # applied is right; running them would be wrong as often as harmless.
    from herald import migrations  # noqa: PLC0415
    stamped = migrations.stamp()
    if stamped:
        pass

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
        warnings.append("the morning brief could not be built: "
                        + (snapshot.stderr or "").strip()[-300:])

    detail = "\n".join(tail)
    if snapshot.returncode == 0:
        lines = len(snapshot.stdout.splitlines())
        detail += f"\n\nBuilt a {lines}-line morning brief from that."

    if answers.get("digest"):
        cycle = subprocess.run([str(config.ROOT / "bin" / "herald"), "cycle", "dawn"],
                               capture_output=True, text=True, timeout=1800)
        if cycle.returncode == 0:
            detail += "\n\n" + (cycle.stdout or "").strip()[-400:]
        else:
            warnings.append("the first digest did not go out: "
                            + (cycle.stderr or "").strip()[-300:])

    unusable = [k for k in capabilities.registry()
                if capabilities.enabled(k) and not capabilities.available(k)]
    for key in unusable:
        warnings.append(f"{key} is switched on but cannot be read yet: "
                        f"{capabilities.missing(key)}")

    return Outcome(ok=True, message="Running.", detail=detail, warnings=warnings)


STEP = Step(key="install", title="Start it running",
            summary="Run it in the background, and show it working",
            status_fn=status, prompt_fn=prompt, apply_fn=apply)
