"""Step 10: what now.

Not a congratulations screen. The three things somebody needs on the day they
finish setup are: what happens next without them doing anything, how to talk to
it, and how to make it stop.
"""

from __future__ import annotations

from herald import capabilities, config, extensions

from .engine import DONE, Field, Outcome, Prompt, State, Step

from . import services


def status(state: State) -> tuple[str, str]:
    return (DONE, "") if state.step("done").get("seen") else ("todo", "")


def prompt(state: State) -> Prompt:
    agent = config.get("agent.name", "Herald")
    who = config.person()
    on = sorted(k for k in capabilities.registry() if capabilities.available(k))
    exts = [e.name for e in extensions.enabled()]
    jobs = services.status()
    telegram = bool(config.secret("telegram.chat_id"))

    lines = [
        f"**{agent} is yours now, {who['first']}.**",
        "",
        "**What happens without you doing anything**",
        "",
        "- every 30 minutes: it reads whatever you connected — "
        + (", ".join(on) or "nothing yet"),
        "- 06:30: a digest of what actually matters that day"
        + (", to Telegram" if telegram else ", as a notification"),
        "- twice a day: it looks for opportunities and deadlines you would "
        "otherwise miss, ranked against your goals",
        "- the moment it finds something closing tomorrow: it interrupts you",
        "",
        "**How to talk to it**",
        "",
    ]
    if telegram:
        lines.append("- message the bot. That is a real conversation with the "
                     "same agent, and it remembers through the ledger rather "
                     "than through the chat.")
    lines += [
        "- `herald attach` on this machine, for a terminal session",
        "- `herald brain url` gives a link that opens the same agent in the "
        "Claude app or claude.ai/code",
        "",
        "**Worth knowing**",
        "",
        "- `herald status` — what it has read, what it cost, what is failing",
        "- `herald db \"select ...\"` — everything it knows, as SQL",
        "- Ask it to change itself. \"Read my library's events feed too\", "
        "\"stop telling me about X\", \"send the digest at 7\" — it edits its own "
        "source, and it knows how.",
        "- Everything about you is in `" + str(config.HOME) + "`. Nothing there "
        "is in the public repository, and you can read or delete any of it.",
        "",
        "**How to stop it**",
        "",
    ]
    if jobs:
        stop = ("systemctl --user stop 'herald-*'" if services.platform_name() == "systemd"
                else "launchctl unload ~/Library/LaunchAgents/com.herald.*.plist")
        lines.append(f"- `{stop}` stops everything immediately.")
    lines.append("- Deleting " + str(config.HOME) + " deletes everything it knows "
                 "about you.")
    if exts:
        lines += ["", f"Extensions running: {', '.join(exts)}."]
    return Prompt(title="Done", blurb="\n".join(lines), fields=[], immediate=True,
                  action="Finish")


def apply(state: State, answers: dict) -> Outcome:
    state.step("done")["seen"] = True
    return Outcome(ok=True, message="Setup complete.")


STEP = Step(key="done", title="Done", summary="What happens next",
            status_fn=status, prompt_fn=prompt, apply_fn=apply)
