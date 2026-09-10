"""Step 10: what now.

Not a congratulations screen. The three things somebody needs on the day they
finish setup are: what happens next without them doing anything, how to talk to
it, and how to make it stop.
"""

from __future__ import annotations

from herald import capabilities, config, extensions

from .sources import friendly

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
    ntfy = bool(config.get("notify.ntfy_topic"))

    lines = [
        f"**{agent} is yours now, {who['first']}.**",
        "",
        "**What happens without you doing anything**",
        "",
        "- every 30 minutes: it reads whatever you connected ("
        + (friendly(on) or "nothing yet") + ")",
        "- 06:30: a digest of what matters that day"
        + (", sent to Telegram" if telegram else
           ", sent as a notification" if ntfy else
           ". **Nothing carries it to your phone yet.** It is written to a file "
           "in your Herald folder and that is all. `herald setup --step "
           "telegram` fixes that in five minutes."),
        "- twice a day: it looks for opportunities and deadlines you would "
        "otherwise miss, judged against your goals",
        "- the moment it finds something closing tomorrow, it tells you",
        "",
        "**How to talk to it**",
        "",
    ]
    if telegram:
        lines.append("- message the bot. That is a real conversation with the "
                     "same agent. It remembers through its notes, not through "
                     "the chat history.")
    lines += [
        "- `herald attach` on this computer, for a conversation in the terminal",
        "- `herald brain url` gives a link that opens the same agent in the "
        "Claude app or at claude.ai/code",
        "",
        "**Worth knowing**",
        "",
        "- `herald status` shows what it has read, what it has cost, and "
        "anything that is not working",
        "- Ask it to change itself. \"Read my library's events feed too\", "
        "\"stop telling me about X\", \"send the digest at 7\". It knows how.",
        "- Everything about you is in `" + str(config.HOME) + "`. None of it "
        "is shared with anyone, and you can read or delete any of it.",
        "",
        "**How to stop it**",
        "",
    ]
    if jobs:
        stop = ("systemctl --user stop 'herald-*'" if services.platform_name() == "systemd"
                else "launchctl unload ~/Library/LaunchAgents/com.herald.*.plist")
        lines.append(f"- `{stop}` stops everything straight away.")
    lines.append("- Deleting the folder " + str(config.HOME) + " deletes everything "
                 "it knows about you.")
    if exts:
        lines += ["", f"Also running: {', '.join(exts)}."]
    return Prompt(title="Done", blurb="\n".join(lines), fields=[], immediate=True,
                  action="Finish")


def apply(state: State, answers: dict) -> Outcome:
    state.step("done")["seen"] = True
    return Outcome(ok=True, message="Setup complete.")


STEP = Step(key="done", title="Done", summary="What happens next",
            status_fn=status, prompt_fn=prompt, apply_fn=apply)
