"""Step 3: the few facts everything else is phrased in terms of.

Deliberately short. This is not the part where Herald learns who somebody is --
that is the interview -- it is the part where it learns what to call them, how
to refer to them in the third person without guessing, and what timezone every
date it ever shows them should be in.

Pronouns are asked rather than inferred. The ledger, the snapshots and the
journal are written *about* the user and read by the agent, so something has to
go in those sentences; a name is not evidence, and a wrong guess misgenders a
real person in a way "they" never does.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import subprocess

from herald import config

from .engine import DONE, TODO, Field, Outcome, Prompt, State, Step

PRONOUN_CHOICES = ["they/them", "she/her", "he/him", "it/its"]


def _guess_timezone() -> str:
    link = pathlib.Path("/etc/localtime")
    if link.is_symlink():
        target = str(link.resolve())
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    try:
        out = subprocess.run(["readlink", "/etc/localtime"], capture_output=True,
                             text=True).stdout.strip()
        if "zoneinfo/" in out:
            return out.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    # /etc/localtime is a copy rather than a link on plenty of machines
    # (this one included). Debian keeps the name in /etc/timezone; systemd
    # answers directly; macOS keeps it in the link above.
    try:
        name = pathlib.Path("/etc/timezone").read_text().strip()
        if name and "/" in name:
            return name
    except OSError:
        pass
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "Timezone", "--value"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        if out and "/" in out:
            return out
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "UTC"


def _own(key: str):
    """A value the person set themselves, ignoring config/defaults.json.

    The shipped default timezone is UTC, and `config.get("timezone")` returns
    it as if it had been chosen -- so the guess from the machine's clock never
    ran and every rehearsal user landed in UTC. Only their own file counts.
    """
    import json  # noqa: PLC0415
    try:
        node = json.loads(config.CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return None
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def status(state: State) -> tuple[str, str]:
    name = config.get("user.name")
    if not name:
        return TODO, "Herald does not know your name yet"
    who = config.person()
    return DONE, (f"{name}, {who['subject']}/{who['object']}, "
                  f"{config.get('timezone')}, agent named "
                  f"{config.get('agent.name', 'Herald')}")


def prompt(state: State) -> Prompt:
    return Prompt(
        title="You, briefly",
        blurb="A few short answers. The longer conversation about who you are "
              "comes later. This is just enough for your agent to refer to you "
              "correctly and to show times in your timezone.",
        fields=[
            Field(key="name", label="Your name", required=True,
                  default=config.get("user.name", ""),
                  help="What it should call you. A first name is fine."),
            Field(key="pronouns", label="Your pronouns", type="choice",
                  choices=PRONOUN_CHOICES,
                  default=config.get("user.pronouns", "they/them"),
                  help="Used when your agent writes about you in its own notes. "
                       "When it talks to you, it says \"you\"."),
            Field(key="timezone", label="Timezone", required=True,
                  default=_own("timezone") or _guess_timezone(),
                  help="Written like Europe/London or America/New_York. We have "
                       "filled in our best guess from this computer's clock."),
            Field(key="email", label="Your email address",
                  default=config.get("user.email", ""),
                  help="Only used to tell your own messages apart from other "
                       "people's in your mailbox. It is never sent anywhere."),
            Field(key="agent_name", label="What to call your agent",
                  default=config.get("agent.name", "Herald"),
                  help="Herald is the name of the program. Yours can have any "
                       "name you like, and it will use it for itself."),
        ])


def apply(state: State, answers: dict) -> Outcome:
    name = (answers.get("name") or "").strip()
    if not name:
        return Outcome(ok=False, message="Your name is the one thing this step needs.")
    tzname = (answers.get("timezone") or "UTC").strip()
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tzname)
    except Exception:                                               # noqa: BLE001
        return Outcome(ok=False,
                       message=f"\"{tzname}\" is not a timezone Herald recognises.",
                       detail="It should look like Europe/London or "
                              "America/Chicago: a region, a slash, and the "
                              "nearest large city.")
    config.set_user("user.name", name)
    config.set_user("user.pronouns", (answers.get("pronouns") or "they/them").strip())
    config.set_user("timezone", tzname)
    if answers.get("email"):
        config.set_user("user.email", answers["email"].strip())
    config.set_user("agent.name", (answers.get("agent_name") or "Herald").strip())

    now = dt.datetime.now(config.tz())
    return Outcome(ok=True,
                   message=f"Hello {config.person()['first']}. It is "
                           f"{now:%H:%M on %A} where you are.")


STEP = Step(key="basics", title="You, briefly",
            summary="Name, pronouns, timezone, and what to call your agent",
            status_fn=status, prompt_fn=prompt, apply_fn=apply)
