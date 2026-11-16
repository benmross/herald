"""What a Telegram topic is running on, and what a settings change keeps, in words.

Every settings reply used to phrase this itself. /claude named the engine but
not the model or thinking level it landed on, /effort named the level but not
the model, and a switch right after /new talked about a transcript that no
longer existed. Someone who did not know how Herald stores sessions could not
tell from the replies what they were using or what a change had cost them.
All of that wording lives here now, so the commands cannot drift apart again.

The ledger is never mentioned: every change keeps it, so saying so is noise.
"""

from __future__ import annotations

import re

from . import config

ENGINE_NAMES = {"claude": "Claude", "codex": "Codex", "kiro": "Kiro"}
# Claude Code accepts these aliases. Each one names a family, not a version.
CLAUDE_ALIASES = {"opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku", "fable": "Fable"}


def engine_name(engine: str | None) -> str:
    return ENGINE_NAMES.get(engine or "", (engine or "?").capitalize())


def _claude_id(model: str) -> str | None:
    """`claude-opus-5-5` or `claude-opus-5.5` -> `Claude Opus 5.5`."""
    m = re.fullmatch(r"claude-([a-z]+)-(\d+)(?:[.-](\d+))?(?:-\d{8})?", model)
    if not m:
        return None
    version = m[2] + (f".{m[3]}" if m[3] else "")
    return f"Claude {m[1].capitalize()} {version}"


def _gpt_id(model: str) -> str | None:
    """`gpt-6.1-sol` -> `GPT-6.1 Sol`."""
    m = re.fullmatch(r"gpt-([\d.]+)(?:-(.+))?", model)
    if not m:
        return None
    rest = " ".join(p.capitalize() for p in (m[2] or "").split("-") if p)
    return f"GPT-{m[1]}" + (f" {rest}" if rest else "")


def model_name(engine: str | None, model: str | None) -> str:
    """A model as a person would say it. Unknown ids are shown as they are."""
    if not model:
        return f"{engine_name(engine)}'s default model"
    pretty = (_claude_id(model) or _gpt_id(model)
              or (f"Claude {CLAUDE_ALIASES[model]}" if model in CLAUDE_ALIASES else None))
    if engine == "kiro":
        return f"Kiro ({pretty or model})"
    if pretty:
        return pretty
    return f"{engine_name(engine)} {model}"


def resolved(ts: dict) -> tuple[str, str | None, str | None]:
    """The engine, model and thinking level the next turn will actually use.

    A topic's own choice wins; otherwise the install's configured default for
    that engine. A level of "auto" means the engine decides, so None here;
    so does a model that neither the topic nor the config names."""
    engine = ts.get("engine") or config.default_engine()
    model = ts.get("model") or config.get(f"engines.{engine}.model")
    effort = ts.get("effort")
    if effort == "auto":
        effort = None
    elif not effort:
        effort = config.get(f"engines.{engine}.effort")
    return engine, model, effort


def thinking(ts: dict) -> str:
    """`high` when the topic chose it; `Default (high)` when it follows a
    default whose value is known; `Default (Codex decides)` when only the
    engine knows. "Default" alone would hide the one thing worth knowing."""
    engine, model, effort = resolved(ts)
    caps = ts.get("effort_caps") or {}
    own = caps.get("engine") == engine and caps.get("model") == ts.get("model")
    if own and caps.get("efforts") == []:
        return "not adjustable on this model"
    chosen = ts.get("effort")
    if chosen and chosen != "auto":
        return chosen
    if effort:
        return f"Default ({effort})"
    native = caps.get("default_effort") if own else None
    return f"Default ({native})" if native else f"Default ({engine_name(engine)} decides)"


def now(ts: dict) -> str:
    """`Model: Claude Opus · Thinking: Default (high)`."""
    engine, model, _ = resolved(ts)
    return f"Model: {model_name(engine, model)} · Thinking: {thinking(ts)}"


def has_conversation(ts: dict) -> bool:
    """Whether there is anything a switch could carry: a live session, or one
    that is about to be summarised into the next."""
    return bool(ts.get("session_id") or ts.get("handoff_from"))


def new_note(ts: dict, had_conversation: bool) -> str:
    """The reply to /new. `ts` is the topic after it was cleared."""
    first = ("New conversation. This conversation and what the topic was working "
             "on are forgotten." if had_conversation else
             "New conversation. There was nothing here to forget.")
    back = " Reply to an older message to go back to it." if had_conversation else ""
    return f"{first}{back}\n{now(ts)}"


def switch_note(ts: dict, before_engine: str, had_conversation: bool,
                level_dropped: str | None = None) -> str:
    """The reply after moving a topic to another engine. `ts` is the topic after."""
    engine = ts.get("engine") or config.default_engine()
    if had_conversation:
        carry = (f"Your next message starts a new conversation, because "
                 f"{engine_name(engine)} can't read {engine_name(before_engine)}'s. "
                 f"It gets a summary of what this topic was working on.")
    else:
        carry = "Your next message starts the conversation."
    dropped = (f" Your earlier choice of {level_dropped} thinking was for "
               f"{engine_name(before_engine)}, so this uses "
               f"{engine_name(engine)}'s setting." if level_dropped else "")
    return f"Switched to {engine_name(engine)}. {carry}{dropped}\n{now(ts)}"


def same_conversation_note(ts: dict, what: str | None = None) -> str:
    """The reply after a change that keeps the conversation: a model on the
    same engine, or a thinking level."""
    lead = f"{what} " if what else ""
    return f"{lead}Same conversation, nothing lost.\n{now(ts)}"


def _ago(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hr ago"
    return f"{hours // 24} days ago"


def _secs(ms) -> str:
    s = (ms or 0) / 1000
    return f"{s:.1f}s" if s < 90 else f"{int(s // 60)}m {int(s % 60)}s"


def context_summary(ts: dict, con=None, *, now_ts: float, running: dict | None = None) -> str:
    """/context: between the status line and /debug. What the topic runs on,
    how much of the context window the conversation fills, what it has cost,
    and what the last turn did, in words with the real numbers.
    `con` reads the conversation's turns from `runs`; it may be None."""
    import datetime as dt
    lines = [now(ts)]
    if running:
        lines.append(f"Working on your message now ({_secs(running['seconds'] * 1000)} so far; "
                     f"its numbers appear when it finishes).")
    session = ts.get("session_id")
    if not session:
        lines.append("No conversation yet: your next message starts one"
                     + (", with a summary of what this topic was working on."
                        if ts.get("handoff_from") else "."))
    else:
        runs = []
        if con is not None:
            runs = con.execute("""
                SELECT ts, duration_ms, cost_usd, input_tokens, output_tokens,
                       cache_read, context_tokens, round_trips
                FROM runs WHERE session_id = ? ORDER BY id""", (session,)).fetchall()
        turns = len(runs) or ts.get("turns", 0)
        parts = [f"{turns} message{'' if turns == 1 else 's'}"]
        if runs and runs[0]["ts"]:
            try:
                parts.append(f"started {_ago(now_ts - dt.datetime.fromisoformat(runs[0]['ts']).timestamp())}")
            except ValueError:
                pass
        cost = sum(r["cost_usd"] or 0 for r in runs)
        if cost:
            parts.append(f"${cost:.2f} so far")
        lines.append("Conversation: " + ", ".join(parts))
        window = ts.get("context_window")
        used = ts.get("context_tokens") or (runs[-1]["context_tokens"] if runs else None)
        share = float(config.get("engines.renewal_context_share", 0.75))
        if window and used:
            lines.append(f"Context: {used:,} of {window:,} tokens ({used / window:.0%}). "
                         f"At {int(window * share):,} ({share:.0%}) the next message "
                         f"starts a new conversation with a summary of this one.")
        elif used:
            lines.append(f"Context: {used:,} tokens.")
        if runs:
            last = runs[-1]
            lines.append(f"Last message: {_secs(last['duration_ms'])}, "
                         f"{last['round_trips'] or '?'} steps; "
                         f"{(last['input_tokens'] or 0):,} new tokens in, "
                         f"{(last['cache_read'] or 0):,} reread from cache, "
                         f"{(last['output_tokens'] or 0):,} out")
    request = (ts.get("work") or {}).get("request")
    if request:
        lines.append(f"Working on: {request}")
    return "\n".join(lines)
