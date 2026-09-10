"""The setup wizard's engine: the steps, their validation, and their state.

Two frontends render this and neither of them decides anything. `setup/cli.py`
asks in a terminal, `setup/web/` asks in a browser, and both call the same
`prompt()` / `apply()` pair on the same step objects. That is not tidiness for
its own sake: a setup process a non-technical person can finish has to be
*verified* at every stage -- "did that actually work" after each screen rather
than an error forty minutes later -- and duplicating twelve verifications
across two frontends would mean two of them drifting apart within a week.

The shape of a step:

    status()          done / todo / blocked, and why, checked live rather than
                      remembered -- a token that has been revoked since setup
                      ran should say so
    prompt()          what to ask right now: prose plus typed fields
    apply(answers)    do it, verify it, and say what happened

Steps with more than one screen (Google's console walkthrough is five) carry a
`stage` in the saved state and return the fields for the stage they are on.
Nothing is ordered by anything except the list at the bottom of this file, and
every step is re-runnable on its own: `herald setup --step google` after a
credential expires must not mean doing the interview again.

State lives in `$HERALD_HOME/setup-state.json`, so a wizard that is interrupted
-- and an hour-long interview will be -- resumes where it stopped.
"""

from __future__ import annotations

import json
import pathlib
import sys
from dataclasses import dataclass, field, asdict
from typing import Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import config  # noqa: E402

STATE_PATH = config.HOME / "setup-state.json"

DONE, TODO, BLOCKED, PARTIAL = "done", "todo", "blocked", "partial"


@dataclass
class Field:
    key: str
    label: str
    #: text | secret | textarea | choice | multichoice | bool | note
    type: str = "text"
    help: str = ""
    default: object = ""
    choices: list = field(default_factory=list)
    required: bool = False
    placeholder: str = ""
    rows: int = 4
    #: dictation makes sense for a long free-write and nowhere else
    dictate: bool = False


@dataclass
class Prompt:
    title: str
    blurb: str = ""
    fields: list[Field] = field(default_factory=list)
    #: what the primary button says
    action: str = "Continue"
    #: markdown shown after the fields -- links, console URLs, warnings
    footnote: str = ""
    #: True when there is nothing to fill in and the step just acts
    immediate: bool = False


@dataclass
class Outcome:
    ok: bool
    message: str = ""
    detail: str = ""
    #: lines worth showing but not worth failing over
    warnings: list = field(default_factory=list)
    #: when a step has more to ask, it says so rather than the frontend guessing
    more: bool = False


class State:
    """The wizard's memory. Small, JSON, and never holding a secret.

    Secrets go straight to `secrets.json` at mode 600 as soon as they are
    validated. Anything half-finished here is a question, not an answer, so a
    stolen setup-state file is worth nothing.
    """

    def __init__(self, path: pathlib.Path | None = None):
        self.path = path or STATE_PATH
        self.data: dict = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except (OSError, json.JSONDecodeError):
                self.data = {}

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value
        self.save()

    def step(self, key: str) -> dict:
        return self.data.setdefault("steps", {}).setdefault(key, {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, default=str))
        tmp.replace(self.path)


@dataclass
class Step:
    key: str
    title: str
    #: one line, for the list of steps
    summary: str
    status_fn: Callable[[State], tuple[str, str]]
    prompt_fn: Callable[[State], Prompt]
    apply_fn: Callable[[State, dict], Outcome]
    #: a step the user can legitimately skip entirely
    optional: bool = False

    def status(self, state: State) -> tuple[str, str]:
        try:
            return self.status_fn(state)
        except Exception as exc:                                    # noqa: BLE001
            return BLOCKED, f"{type(exc).__name__}: {exc}"

    def prompt(self, state: State) -> Prompt:
        return self.prompt_fn(state)

    def apply(self, state: State, answers: dict) -> Outcome:
        try:
            outcome = self.apply_fn(state, answers)
        except Exception as exc:                                    # noqa: BLE001
            return Outcome(ok=False, message=f"{type(exc).__name__}: {exc}")
        state.save()
        return outcome


def steps() -> list[Step]:
    """Every step, in order.

    Imported here rather than at module scope so that a broken step module
    cannot stop `herald setup` from starting up and reporting that it is
    broken.
    """
    from . import (basics, done, google, home, install, interview,  # noqa: PLC0415
                   preflight, rules, sources, telegram)
    return [
        preflight.STEP,
        home.STEP,
        basics.STEP,
        google.STEP,
        telegram.STEP,
        sources.STEP,
        interview.STEP,
        rules.STEP,
        install.STEP,
        done.STEP,
    ]


def by_key(key: str) -> Step | None:
    for step in steps():
        if step.key == key:
            return step
    return None


def overview(state: State) -> list[dict]:
    """Every step with its live status, for a progress list."""
    out = []
    for step in steps():
        status, detail = step.status(state)
        out.append({"key": step.key, "title": step.title, "summary": step.summary,
                    "status": status, "detail": detail, "optional": step.optional})
    return out


def next_step(state: State) -> Step | None:
    """The first step that is not done. Blocked counts as not done: a blocked
    preflight is exactly where someone should be sent."""
    for step in steps():
        status, _ = step.status(state)
        if status != DONE:
            return step
    return None


def as_dict(prompt: Prompt) -> dict:
    return {"title": prompt.title, "blurb": prompt.blurb, "action": prompt.action,
            "footnote": prompt.footnote, "immediate": prompt.immediate,
            "fields": [asdict(f) for f in prompt.fields]}
