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
    return "UTC"


def status(state: State) -> tuple[str, str]:
    name = config.get("user.name")
    if not name:
        return TODO, "Herald does not know your name yet"
    who = config.person()
    return DONE, (f"{name}, {who['subject']}/{who['object']}, "
                  f"{config.get('timezone')}, agent called "
                  f"{config.get('agent.name', 'Herald')}")


def prompt(state: State) -> Prompt:
    return Prompt(
        title="You, briefly",
        blurb="Four things. The long version comes later — this is just enough "
              "for your agent to talk about you correctly and put your days in "
              "the right timezone.",
        fields=[
            Field(key="name", label="Your name", required=True,
                  default=config.get("user.name", ""),
                  help="What it should call you. A first name is fine."),
            Field(key="pronouns", label="Your pronouns", type="choice",
                  choices=PRONOUN_CHOICES,
                  default=config.get("user.pronouns", "they/them"),
                  help="Used when your agent writes *about* you — in its own "
                       "notes, and in the brief it reads each morning. Anything "
                       "it writes *to* you is second person regardless."),
            Field(key="timezone", label="Timezone", required=True,
                  default=config.get("timezone") or _guess_timezone(),
                  help="An IANA name like Europe/London or America/New_York. "
                       "Every time Herald ever shows you is in this."),
            Field(key="email", label="Your email address",
                  default=config.get("user.email", ""),
                  help="Only used to recognise your own messages in your own "
                       "mailbox. Herald never sends it anywhere."),
            Field(key="agent_name", label="What to call your agent",
                  default=config.get("agent.name", "Herald"),
                  help="Herald is the project. Your instance can be called "
                       "whatever you like — it will use this name for itself."),
        ])


def apply(state: State, answers: dict) -> Outcome:
    name = (answers.get("name") or "").strip()
    if not name:
        return Outcome(ok=False, message="A name is the one thing this step needs.")
    tzname = (answers.get("timezone") or "UTC").strip()
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tzname)
    except Exception:                                               # noqa: BLE001
        return Outcome(ok=False,
                       message=f"{tzname!r} is not a timezone name.",
                       detail="It should look like Europe/London or "
                              "America/Chicago. `timedatectl` or the Date & "
                              "Time settings will tell you which one you are in.")
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
