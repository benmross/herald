"""Choose one first-party provider or both before checking any model CLI."""
from __future__ import annotations

from herald import config
from .engine import DONE, TODO, Field, Outcome, Prompt, State, Step

CHOICES = {"Claude only": ["claude"], "Codex only": ["codex"], "Both": ["claude", "codex"]}


def status(state: State) -> tuple[str, str]:
    try:
        engine = config.default_engine()
    except ValueError:
        return TODO, "Choose Claude, Codex, or both"
    # Legacy installs get an explicit default through the update migration.
    return DONE, f"{', '.join(config.enabled_engines())}; default: {engine}"


def prompt(state: State) -> Prompt:
    enabled = config.get("engines.enabled")
    current = next((label for label, names in CHOICES.items() if enabled == names), "Choose providers")
    return Prompt(title="Your model providers",
        blurb="Herald runs the first-party CLI on your subscription. Choose Claude "
              "only, Codex only, or both. Either one supports Telegram, terminal "
              "conversations and scheduled jobs. The next page checks only the "
              "providers you choose and gives the install and sign-in commands.",
        fields=[Field(key="providers", label="Providers", type="choice", required=True,
                      choices=["Choose providers", *CHOICES], default=current),
                Field(key="default_engine", label="Default provider (when using both)", type="choice",
                      choices=["Choose if using both", "claude", "codex"],
                      default=config.get("engines.default_engine") or "Choose if using both",
                      help="Used for scheduled jobs, the terminal and new Telegram topics."),
                Field(key="remote_control", label="Enable Claude Remote Control", type="bool",
                      default=config.remote_control_enabled(),
                      help="Optional Claude app/web surface, available when Claude is selected. "
                           "Leave off if you only want Telegram or the terminal.")])


def apply(state: State, answers: dict) -> Outcome:
    enabled = CHOICES.get(answers.get("providers"))
    if not enabled:
        return Outcome(ok=False, message="Choose Claude only, Codex only, or both.")
    default = enabled[0] if len(enabled) == 1 else answers.get("default_engine")
    if default not in enabled:
        return Outcome(ok=False, message="Choose your default provider when using both.")
    if answers.get("remote_control") and "claude" not in enabled:
        return Outcome(ok=False, message="Claude Remote Control requires selecting Claude.")
    was_remote = config.remote_control_enabled()
    config.set_users({"engines.enabled": enabled, "engines.default_engine": default,
                      "surfaces.remote_control.enabled": bool(answers.get("remote_control"))})
    warnings = []
    if was_remote and not config.remote_control_enabled():
        from . import services
        warnings = services.disable_remote_control()
    state.step("providers")["chosen"] = True
    message = f"Using {', '.join(enabled)}. Default: {default}."
    if config.remote_control_enabled() and not was_remote:
        message += " The Start it running step enables Remote Control (`herald services enable` on an existing install)."
    return Outcome(ok=True, message=message, warnings=warnings)


STEP = Step(key="providers", title="Your model providers", summary="Claude only, Codex only, or both",
            status_fn=status, prompt_fn=prompt, apply_fn=apply)
