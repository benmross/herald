"""The one place Herald reaches a model — by running a first-party CLI.

Nothing here speaks HTTP to a model API. `claude` (Claude Code) or `codex`
(the Codex CLI) is launched as a subprocess and authenticates itself against
the user's own subscription. That is what makes this legitimate under either
vendor's terms (subscription OAuth is authorized for the first-party tools
only, never for third-party callers), and it is why this module must never be
"optimized" into an SDK client.

Two engines, chosen, never fallen back to. Herald used to hand an interrupted
run to `codex exec` with a saved work record when Claude hit a usage limit;
that came out on 9 September 2026 because the redundancy was not worth the
amount of logic it took to carry a transcript handoff between two engines.
What came back on 22 September 2026 is narrower and different in kind: the
caller *picks* an engine (`engine="codex"`, `/codex` in Telegram, or
`engines.default_engine` in the config) and the run lives and dies there. A
usage limit or a timeout is still a failure with a clear error, not a run
that quietly finishes somewhere else, and a session is never resumed across
engines because neither can read the other's transcript.

The two are made to look the same from the outside -- one `Result`, one
`Progress` stream, one `runs` row -- so that nothing above this module knows
which one ran. The differences that could not be hidden are listed on
`_run_codex`.

Every invocation is metered into the `runs` table.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, db

# Result text or API status that means "out of quota". Nothing acts on this any
# more except the error message: a caller told "usage limit" knows to wait,
# where a caller told "exit 1" goes looking for a bug that is not there.
_RATE_LIMIT_STATUSES = {429, 529}
_RATE_LIMIT_HINTS = ("rate limit", "usage limit", "quota", "overloaded",
                     "too many requests", "capacity", "session limit", "rate_limit")

# A prompt longer than this is written to a file and referenced by path instead
# of passed on argv.
_ARGV_PROMPT_LIMIT = 120_000

ENGINES = ("claude", "codex")

# Herald's own vocabulary for how hard the model should think, which is Claude
# Code's `--effort` scale. Codex's `model_reasoning_effort` has no "max" and
# adds "minimal" below "low"; `codex_effort()` maps between them.
EFFORTS = ("low", "medium", "high", "xhigh", "max")
_CODEX_EFFORTS = {"minimal": "minimal", "low": "low", "medium": "medium",
                  "high": "high", "xhigh": "xhigh", "max": "xhigh"}


def normalize_effort(level: str | None) -> str | None:
    """Canonical effort name, or None for "the engine's own default".
    Raises ValueError on anything that is not a level either engine knows."""
    if level is None or level == "":
        return None
    level = level.strip().lower()
    if level in EFFORTS or level in _CODEX_EFFORTS:
        return level
    raise ValueError(f"unknown effort {level!r}; one of {', '.join(EFFORTS)}")


def codex_effort(level: str | None) -> str | None:
    return _CODEX_EFFORTS.get(level) if level else None


def claude_effort(level: str | None) -> str | None:
    # "minimal" is Codex-only; Claude's floor is "low".
    return "low" if level == "minimal" else level


def resolve_engine(engine: str | None) -> str:
    """The engine a call runs on: the one asked for, else the configured
    default, else claude. Unknown names are a ValueError rather than a
    silent fallback -- the whole point is that the engine is a choice."""
    engine = (engine or config.get("engines.default_engine") or "claude").lower()
    if engine not in ENGINES:
        raise ValueError(f"unknown engine {engine!r}; one of {', '.join(ENGINES)}")
    return engine

# Subprocesses currently in flight, keyed by whatever the caller wants to be
# able to interrupt them by -- herald-telegram uses its topic key, so `/stop`
# can reach a specific conversation's process. One entry per key: a topic only
# ever has one turn running at a time (its own lock in herald-telegram
# enforces that), so a second registration under the same key never happens
# in practice, but `cancel()` only ever acts on whatever is in here now.
_active_lock = threading.Lock()
_active: dict[str, dict] = {}


def cancel(key: str) -> bool:
    """Terminate the engine subprocess running under `key`, if any.

    This is the only way to interrupt a `think()` call already in progress --
    the call itself blocks the calling thread until the subprocess exits, so
    whatever wants to stop it has to reach the process directly, from a
    different thread. Returns True if something was found and signalled,
    False if `key` had nothing running (already finished, or never started).
    """
    with _active_lock:
        entry = _active.get(key)
    if entry is None or entry["proc"].poll() is not None:
        return False
    entry["cancelled"].set()
    try:
        if entry.get("process_group"):
            os.killpg(entry["proc"].pid, signal.SIGTERM)
        else:
            entry["proc"].terminate()
    except ProcessLookupError:
        pass  # it completed between poll() and the signal
    return True


def steer(key: str, text: str) -> bool:
    """Inject an additional user message into the live run under `key`, if
    it's still accepting them. Returns whether it landed -- False means
    nothing is running under `key`, or what was running has already committed
    to finalizing, and the caller should start a fresh turn instead.

    Checking "is it still accepting" and enqueueing the message happen under
    the same `_active_lock` acquisition as the one place that flips
    `accepting_steers` to False (`_run_claude`, right after it sees a `result`
    event with nothing queued). That's deliberate: a message arriving in the
    gap between "the run decided to close" and "the run actually closed"
    would otherwise land in a queue nobody reads again, silently dropped --
    worse than the caller falling back to a normal new turn.

    A Codex run never accepts steers: `codex exec` reads its prompt from stdin
    once and closes it, so there is no channel to fold a second message into.
    The caller gets False and starts a fresh turn, which is the same path a
    Claude run takes once it has committed to finalizing.
    """
    with _active_lock:
        entry = _active.get(key)
        if entry is None or not entry.get("accepting_steers"):
            return False
        entry["steer_queue"].put(text)
        return True


def current_progress(key: str) -> str:
    """Whatever `_tool_summary` last reported for the live run under `key`,
    or "" if there is none -- so a caller telling the user their steer landed can
    say what it landed after."""
    with _active_lock:
        entry = _active.get(key)
    return (entry or {}).get("progress", "")


@dataclass
class Result:
    ok: bool
    text: str = ""
    engine: str = ""
    model: str | None = None
    session_id: str | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    structured: dict | None = None
    error: str | None = None
    cancelled: bool = False
    rate_limited: bool = False
    denials: list = field(default_factory=list)

    def json(self) -> dict | None:
        """The reply parsed as JSON, whether it came back structured or fenced."""
        if self.structured is not None:
            return self.structured
        text = self.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None


def _looks_rate_limited(exit_code: int, payload: dict | None, stderr: str) -> bool:
    if payload:
        if payload.get("api_error_status") in _RATE_LIMIT_STATUSES:
            return True
        blob = f"{payload.get('result', '')} {payload.get('subtype', '')}".lower()
    else:
        blob = ""
    blob += " " + (stderr or "").lower()
    if exit_code != 0 and any(h in blob for h in _RATE_LIMIT_HINTS):
        return True
    return any(h in blob for h in _RATE_LIMIT_HINTS) and (exit_code != 0 or not payload
                                                          or payload.get("is_error"))


def _stage_prompt(prompt: str, workdir: Path) -> tuple[str, Path | None]:
    """Keep very large prompts off argv."""
    if len(prompt) <= _ARGV_PROMPT_LIMIT:
        return prompt, None
    config.RAW.mkdir(parents=True, exist_ok=True)
    fh = tempfile.NamedTemporaryFile("w", suffix=".prompt.md", dir=config.RAW,
                                     delete=False, encoding="utf-8")
    fh.write(prompt)
    fh.close()
    path = Path(fh.name)
    return (f"Read the file at {path} in full. It contains your complete "
            f"instructions for this task. Follow them."), path


@dataclass
class Progress:
    """One thing that just happened inside a running turn.

    `kind` is "text" for the model's own narration -- the sentences it writes
    between tool calls, explaining what it is about to do and what it found --
    and "tool" for a call it made. Both come out of the stream-json output that
    is already flowing, so neither costs a token or a millisecond.

    The narration used to be dropped on the floor here, which is why watching a
    turn from Telegram felt so much worse than watching one in a terminal: the
    terminal shows what the model is thinking through, and Telegram was showing
    a list of file paths.
    """
    kind: str
    text: str


def _text_summary(block: dict) -> str:
    """The model's narration, whole.

    Not truncated and not collapsed. It was both at first -- 400 characters,
    newlines squashed -- which quietly dropped the end of any longer thought,
    and dropping text is exactly what a progress display must not do. Whoever
    renders this decides how much of it fits; that is a display question, and
    the answer to it should not be baked in here where nothing can recover
    what was cut.
    """
    return (block.get("text") or "").strip()


def _tool_summary(block: dict) -> str:
    """A short human-readable line for one tool_use block -- 'Bash: curl ...',
    'Read: menu.py', not the full call. Used only for a live progress snippet;
    never shown to the model, never costs a token."""
    name = block.get("name") or "tool"
    tool_input = block.get("input") or {}
    for key in ("command", "file_path", "path", "url", "query", "pattern", "prompt"):
        val = tool_input.get(key)
        if val:
            val = str(val)
            if len(val) > 60:
                val = val[:57] + "..."
            return f"{name}: {val}"
    return name


def _mark(state: dict, kind: str, name: str | None, since: float,
          detail: str | None = None) -> None:
    """Close one phase of the run at the current instant.

    `since` is when the phase began; everything is monotonic seconds, stored as
    integer milliseconds. Phases are appended in the order they close, which is
    also the order they happened, because a run is strictly sequential from the
    outside: think, call a tool, wait, think again.
    """
    now = time.monotonic()
    ms = int((now - since) * 1000)
    if ms < 0:
        ms = 0
    state.setdefault("phases", []).append(
        {"kind": kind, "name": name, "ms": ms, "detail": detail})
    state["mark"] = now


def _handle_stream_line(line: str, state: dict, on_progress,
                        active_key: str | None = None) -> None:
    """Parse one stream-json line, updating `state` in place, firing
    `on_progress` for a tool call, and (if `active_key` is registered)
    recording the same summary into `_active` so `current_progress()` can
    read it from a different thread -- shared by the live loop and (nothing
    else, now, but kept separate so the parsing logic has one home).

    It also times the run, because this function already sees every boundary
    that matters and adding a clock here costs nothing. The stream is strictly
    ordered -- `system/init`, then alternating `assistant` (the model finished
    thinking) and `user` carrying `tool_result` (a tool finished running) --
    which is verified against the real CLI rather than assumed. That ordering
    is the whole reason a timeline can be recovered from a single pass with no
    extra process and no extra tokens.
    """
    try:
        candidate = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return
    if not isinstance(candidate, dict):
        return
    kind_top = candidate.get("type")
    mark = state.get("mark") or state.get("t0")

    if kind_top == "system" and candidate.get("subtype") == "init":
        # Process launch through to the CLI being ready. The only phase that is
        # pure overhead: no model, no tool, nothing the prompt can shorten.
        if state.get("t0") is not None and not state.get("saw_init"):
            state["saw_init"] = True
            _mark(state, "startup", None, state["t0"])
        return

    if kind_top == "user":
        # A tool result coming back. `--replay-user-messages` also echoes the
        # prompt itself as a `user` event, which carries no tool_result and so
        # closes nothing -- checked rather than assumed, because counting it
        # would attribute the model's first think to a tool.
        for block in (candidate.get("message") or {}).get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            pend = state.setdefault("pending_tools", {}).pop(
                block.get("tool_use_id"), None)
            if pend:
                _mark(state, "tool", pend[0], pend[1], pend[2])
        return

    if kind_top == "result":
        state["payload"] = candidate
        # Only if something actually elapsed. `result` normally lands in the
        # same millisecond as the final `assistant` event, which already closed
        # that think; the guard exists for a run that ends without one.
        if mark is not None and time.monotonic() - mark > 0.05:
            _mark(state, "model", None, mark)
        return

    if kind_top == "assistant":
        message = candidate.get("message") or {}
        state["last_assistant_usage"] = message.get("usage")
        # The model just finished a round trip. Everything since the previous
        # boundary was it thinking and generating.
        if mark is not None:
            _mark(state, "model", None, mark)
        state["round_trips"] = state.get("round_trips", 0) + 1
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_use":
                summary = _tool_summary(block)
            elif kind == "text":
                summary = _text_summary(block)
                if not summary:
                    continue
            else:
                continue
            if on_progress:
                on_progress(Progress("tool" if kind == "tool_use" else "text",
                                     summary))
            if kind == "tool_use":
                state.setdefault("pending_tools", {})[block.get("id")] = (
                    block.get("name") or "tool", time.monotonic(), summary)
            if kind == "tool_use" and active_key:
                # `current_progress` answers "what is it doing right now", so
                # it tracks the tool call rather than the commentary around it.
                with _active_lock:
                    entry = _active.get(active_key)
                    if entry is not None:
                        entry["progress"] = summary


def _run_claude(prompt: str, *, model: str, effort: str | None, cwd: Path,
                timeout: int, idle_timeout: int,
                allowed_tools: list[str] | None, append_system_prompt: str | None,
                json_schema: dict | None, resume: str | None,
                permission_mode: str, add_dirs: list[str] | None,
                on_progress=None, cancel_key: str | None = None,
                submitted: list[str] | None = None,
                timing: dict | None = None,
               ) -> tuple[int, dict | None, int | None, str, str, bool]:
    staged, tmp = _stage_prompt(prompt, cwd)

    # stream-json instead of json for one reason: the final "result" event is
    # byte-for-byte the same payload either way (verified directly -- same
    # keys, same total_cost_usd, same usage -- so every existing call below
    # that reads `payload` keeps working unchanged), but stream-json *also*
    # emits one line per individual assistant message as the run happens, each
    # carrying that specific API call's own usage. The aggregate "result"
    # usage is a sum across every internal step of the run and answers "what
    # did this run cost" -- it cannot answer "how big is the conversation
    # right now", which is a different question `context_tokens` below exists
    # to answer. `--verbose` is not optional: the CLI refuses stream-json
    # under --print without it.
    #
    # --input-format stream-json (rather than the prompt on argv) is what
    # makes mid-turn steering possible: it's the only input mode that leaves
    # stdin open for more than one message. Tested directly against the real
    # CLI (2.1.263) before trusting it -- a second JSON line written to stdin
    # while a tool call is in flight is folded into the turn already running
    # (one `result` event, `turns > 1`), not queued as a separate turn.
    # --replay-user-messages echoes each injected message back on stdout for
    # confirmation; not consumed yet, but costs nothing to enable now.
    cmd = ["claude", "-p",
           "--input-format", "stream-json",
           "--output-format", "stream-json", "--verbose",
           "--replay-user-messages",
           "--model", model,
           "--permission-mode", permission_mode,
           # Nobody is at the terminal. Without this the run blocks forever on
           # anything that would have prompted.
           "--permission-prompts", "none"]

    if effort:
        cmd += ["--effort", effort]
    if allowed_tools:
        cmd += ["--allowedTools", ",".join(allowed_tools)]
    if append_system_prompt:
        cmd += ["--append-system-prompt", append_system_prompt]
    if json_schema:
        cmd += ["--json-schema", json.dumps(json_schema)]
    if resume:
        cmd += ["--resume", resume]
    for d in add_dirs or []:
        cmd += ["--add-dir", d]

    def _write(proc, text: str) -> None:
        line = json.dumps({"type": "user",
                           "message": {"role": "user",
                                       "content": [{"type": "text", "text": text}]}})
        proc.stdin.write(line + "\n")
        proc.stdin.flush()

    def _finish(state: dict) -> tuple[dict | None, int | None]:
        last_assistant_usage = state.get("last_assistant_usage")
        context_tokens = None
        if last_assistant_usage:
            context_tokens = (
                (last_assistant_usage.get("input_tokens") or 0)
                + (last_assistant_usage.get("cache_read_input_tokens") or 0)
                + (last_assistant_usage.get("cache_creation_input_tokens") or 0)
            )
        return state["payload"], context_tokens

    try:
        return _run_process("claude", cmd, staged, write=_write, steerable=True,
                            handler=_handle_stream_line, finish=_finish,
                            cwd=cwd, timeout=timeout, idle_timeout=idle_timeout,
                            on_progress=on_progress, cancel_key=cancel_key,
                            submitted=submitted, timing=timing)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _toml_string(text: str) -> str:
    """`text` as a TOML basic string, for `codex -c key="..."`.

    JSON's string grammar is a subset of TOML's basic-string grammar -- the
    same backslash escapes, control characters as \\uXXXX, and json.dumps
    never emits the one escape (\\/) TOML lacks -- so this is json.dumps by
    another name. Named so the intent is visible at the call site.
    """
    return json.dumps(text)


def _strict_schema(schema):
    """`schema` rewritten to what OpenAI's structured output insists on.

    Codex passes `--output-schema` to the Responses API in strict mode, which
    rejects any object without `additionalProperties: false` and any object
    whose `required` does not list every property (verified against the real
    CLI, 0.153.4: a schema shaped the way `cycles/dawn.py` writes it comes
    back as an `invalid_json_schema` 400). Claude accepts the looser form and
    is stricter the other way -- `additionalProperties` is not wanted there.

    So the cycles keep writing one schema in the Anthropic dialect, and this
    derives the OpenAI one: every object closed, every property required, and
    the properties that were *optional* made nullable so the model can still
    leave them out by answering null. `Result.json()` callers already treat
    a missing key and None alike (`payload.get("questions") or []`), which is
    what makes that substitution safe.
    """
    if isinstance(schema, list):
        return [_strict_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: _strict_schema(v) for k, v in schema.items()}
    if out.get("type") == "object" or "properties" in out:
        props = out.get("properties") or {}
        was_required = set(out.get("required") or [])
        for name, prop in props.items():
            if name not in was_required and isinstance(prop, dict):
                props[name] = _nullable(prop)
        out["properties"] = props
        out["required"] = list(props.keys())
        out["additionalProperties"] = False
    return out


def _nullable(prop: dict) -> dict:
    """One property schema, widened to also accept null."""
    prop = dict(prop)
    t = prop.get("type")
    if isinstance(t, str) and t != "null":
        prop["type"] = [t, "null"]
    elif isinstance(t, list) and "null" not in t:
        prop["type"] = [*t, "null"]
    if "enum" in prop and None not in prop["enum"]:
        prop["enum"] = [*prop["enum"], None]
    return prop


def _codex_item_summary(item: dict) -> tuple[str, str]:
    """(tool name, one-line summary) for a Codex stream item, in the same
    shape `_tool_summary` gives a Claude tool_use block, so the progress
    display and the run_phases table read the same for both engines."""
    kind = item.get("type") or "tool"
    if kind == "command_execution":
        cmd = str(item.get("command") or "")
        # Codex wraps every command as `/bin/bash -lc '<cmd>'`; the wrapper
        # is noise in a progress line.
        if cmd.startswith("/bin/bash -lc "):
            cmd = cmd[len("/bin/bash -lc "):].strip("'")
        name, val = "Bash", cmd
    elif kind == "file_change":
        paths = [c.get("path", "") for c in item.get("changes") or []
                 if isinstance(c, dict)]
        name, val = "Edit", ", ".join(p for p in paths if p)
    elif kind == "mcp_tool_call":
        name = f"mcp__{item.get('server', '')}__{item.get('tool', '')}"
        val = json.dumps(item.get("arguments") or {}) if item.get("arguments") else ""
    elif kind == "web_search":
        name, val = "WebSearch", str(item.get("query") or "")
    else:
        name, val = kind, ""
    if len(val) > 60:
        val = val[:57] + "..."
    return name, f"{name}: {val}" if val else name


def _handle_codex_line(line: str, state: dict, on_progress,
                       active_key: str | None = None) -> None:
    """Parse one line of `codex exec --json`, the way `_handle_stream_line`
    parses `claude -p --output-format stream-json`, into the same `state`.

    The stream (verified against codex-cli 0.153.4, not taken from the docs
    alone) is: `thread.started` carrying the thread id, `turn.started`, then
    items -- `item.started`/`item.completed` pairs for a command, file change,
    MCP call or web search, and lone `item.completed` events for the model's
    own text (`agent_message`, `reasoning`) -- and finally `turn.completed`
    with the turn's usage, or `turn.failed` with an error. A bare `error`
    event may precede `turn.failed`.

    The final payload is synthesised in the *Claude* result shape (`result`,
    `session_id`, `is_error`, `usage`, `num_turns`) so `think()` handles one
    shape. Two honest gaps: Codex reports no dollar cost, and its usage is a
    sum over the turn, so `context_tokens` -- the size of the last prompt --
    cannot be recovered from it and stays None.
    """
    try:
        candidate = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return
    if not isinstance(candidate, dict):
        return
    kind_top = candidate.get("type")
    mark = state.get("mark") or state.get("t0")

    if kind_top == "thread.started":
        state["session_id"] = candidate.get("thread_id")
        if state.get("t0") is not None and not state.get("saw_init"):
            state["saw_init"] = True
            _mark(state, "startup", None, state["t0"])
        return

    if kind_top in ("item.started", "item.completed"):
        item = candidate.get("item") or {}
        item_id = item.get("id")
        pending = state.setdefault("pending_tools", {})
        if kind_top == "item.completed" and item_id in pending:
            pend = pending.pop(item_id)
            _mark(state, "tool", pend[0], pend[1], pend[2])
            return
        if item.get("type") == "error":
            # The CLI talking, not the model: "hook trust bypassed", "hooks
            # config failed to parse". Kept for the error message, and not
            # counted as a round trip, because no model call produced it.
            state.setdefault("notices", []).append(item.get("message") or "")
            return
        # Anything else arriving is the model having finished a stretch of
        # thinking: a tool it is about to run, or text it wrote. A round trip
        # is counted per stretch rather than per item, because one reply can
        # carry a message and a command from the same model call.
        if mark is not None and time.monotonic() - mark > 0.05:
            _mark(state, "model", None, mark)
            state["round_trips"] = state.get("round_trips", 0) + 1
        item_kind = item.get("type")
        if item_kind in ("agent_message", "reasoning"):
            text = (item.get("text") or "").strip()
            if item_kind == "agent_message":
                state["last_text"] = item.get("text") or ""
            if text and on_progress:
                on_progress(Progress("text", text))
            return
        if kind_top == "item.started":
            name, summary = _codex_item_summary(item)
            if on_progress:
                on_progress(Progress("tool", summary))
            pending[item_id] = (name, time.monotonic(), summary)
            if active_key:
                with _active_lock:
                    entry = _active.get(active_key)
                    if entry is not None:
                        entry["progress"] = summary
        return

    if kind_top == "error":
        state["error"] = candidate.get("message") or "error"
        return

    if kind_top in ("turn.completed", "turn.failed"):
        if mark is not None and time.monotonic() - mark > 0.05:
            _mark(state, "model", None, mark)
        usage = candidate.get("usage") or {}
        failed = kind_top == "turn.failed"
        error = (candidate.get("error") or {}).get("message") or state.get("error")
        state["payload"] = {
            "type": "result",
            "is_error": failed,
            "result": error if failed else (state.get("last_text") or ""),
            "session_id": state.get("session_id"),
            "usage": {"input_tokens": usage.get("input_tokens"),
                      "output_tokens": usage.get("output_tokens"),
                      "cache_read_input_tokens": usage.get("cached_input_tokens"),
                      "cache_creation_input_tokens": usage.get("cache_write_input_tokens")},
            "num_turns": state.get("round_trips") or None,
        }
        return


def _run_codex(prompt: str, *, model: str | None, effort: str | None, cwd: Path,
               timeout: int, idle_timeout: int,
               append_system_prompt: str | None, json_schema: dict | None,
               resume: str | None, permission_mode: str,
               add_dirs: list[str] | None,
               on_progress=None, cancel_key: str | None = None,
               submitted: list[str] | None = None,
               timing: dict | None = None,
              ) -> tuple[int, dict | None, int | None, str, str, bool]:
    """`codex exec`, shaped to match `_run_claude` from the outside.

    What could not be made identical, so a caller knows what it is getting:

    - **No `allowed_tools`.** Codex has no per-tool allowlist; what it may do
      is the sandbox and approval policy (`engines.codex.sandbox`,
      `engines.codex.approval_policy`), plus the same guard hook Claude runs
      (`.codex/hooks.json`, written by `herald ext sync` from the same source
      as `.claude/settings.json`). `permission_mode="plan"` is honoured as a
      read-only sandbox; every other mode means the configured one.
    - **No steering.** The prompt goes in on stdin and stdin closes.
    - **No cost, no context size.** See `_handle_codex_line`.
    - **Instructions.** Codex reads `AGENTS.md`, not `CLAUDE.md`, so
      `project_doc_fallback_filenames` is set to pick up the constitution
      Herald already generates; `append_system_prompt` becomes
      `developer_instructions`, which is what the docs offer in place of a
      system-prompt flag. Skills come from `.agents/skills/`, which
      `herald ext sync` keeps equal to `.claude/skills/`.
    - **Hook trust.** Codex will not run a hook it has not been asked to
      trust interactively. `--dangerously-bypass-hook-trust` runs the ones
      in the generated file without that ceremony; they are Herald's own
      guard and the enabled extensions' hooks, the same set Claude runs, and
      a guard that silently does not run is the worse outcome.
    """
    sub = ["exec"]
    if resume:
        sub += ["resume", resume]
    cmd = ["codex", *sub, "--json",
           # Nobody is at the terminal; a git check or a trust prompt would
           # block a cycle forever.
           "--skip-git-repo-check",
           "--dangerously-bypass-hook-trust",
           "-c", "project_doc_fallback_filenames=" + json.dumps(["CLAUDE.md"])]
    if permission_mode == "plan":
        cmd += ["-c", 'sandbox_mode="read-only"']
    else:
        cmd += ["-c", "sandbox_mode=" + _toml_string(
                    config.get("engines.codex.sandbox", "danger-full-access")),
                "-c", "approval_policy=" + _toml_string(
                    config.get("engines.codex.approval_policy", "never"))]
    if model:
        cmd += ["-m", model]
    if effort:
        cmd += ["-c", "model_reasoning_effort=" + _toml_string(effort)]
    if append_system_prompt:
        cmd += ["-c", "developer_instructions=" + _toml_string(append_system_prompt)]
    for d in add_dirs or []:
        cmd += ["--add-dir", d]

    schema_file = None
    if json_schema:
        config.RAW.mkdir(parents=True, exist_ok=True)
        fh = tempfile.NamedTemporaryFile("w", suffix=".schema.json", dir=config.RAW,
                                         delete=False, encoding="utf-8")
        json.dump(_strict_schema(json_schema), fh)
        fh.close()
        schema_file = Path(fh.name)
        cmd += ["--output-schema", str(schema_file)]
    # The prompt itself: "-" reads it from stdin, which has no argv limit and
    # keeps a long snapshot out of `ps`.
    cmd.append("-")

    def _write(proc, text: str) -> None:
        proc.stdin.write(text)
        proc.stdin.flush()
        proc.stdin.close()

    def _finish(state: dict) -> tuple[dict | None, int | None]:
        payload = state.get("payload")
        if payload is None and state.get("error"):
            # Died after an `error` event without ever reaching turn.failed.
            payload = {"type": "result", "is_error": True,
                       "result": state["error"],
                       "session_id": state.get("session_id")}
        if payload and json_schema and not payload.get("is_error"):
            try:
                payload["structured_output"] = json.loads(payload.get("result") or "")
            except (json.JSONDecodeError, ValueError):
                pass
        return payload, None

    try:
        return _run_process("codex", cmd, prompt, write=_write, steerable=False,
                            handler=_handle_codex_line, finish=_finish,
                            cwd=cwd, timeout=timeout, idle_timeout=idle_timeout,
                            on_progress=on_progress, cancel_key=cancel_key,
                            submitted=submitted, timing=timing)
    finally:
        if schema_file is not None:
            schema_file.unlink(missing_ok=True)


def _run_process(engine: str, cmd: list[str], prompt: str, *, write, steerable: bool,
                 handler, finish, cwd: Path, timeout: int, idle_timeout: int,
                 on_progress=None, cancel_key: str | None = None,
                 submitted: list[str] | None = None,
                 timing: dict | None = None,
                ) -> tuple[int, dict | None, int | None, str, str, bool]:
    """Launch `cmd`, feed it `prompt`, and read its event stream until it ends.

    Engine-neutral on purpose: `write` knows how this engine takes a message,
    `handler` knows how to read one line of its stream, and `finish` turns
    the parsed state into (payload, context_tokens). Everything else --
    the two deadlines, cancellation, steering, timing -- is the same for both
    and lives here once.

    Read stdout as it arrives rather than waiting for the process to exit and
    capturing it whole. The bytes are identical either way -- this changes
    nothing about what runs or what it costs -- but both engines emit one
    line per step as the run happens, and a caller (herald-telegram's
    "still on it" notice) can watch `on_progress` for a free, live "what is
    it doing right now" with no extra tokens and no extra round trip.

    `select` gives a timeout on a blocking read without a second thread for
    stdout; stderr still needs its own thread; a single Popen's two pipes
    can deadlock if only one is drained while the other fills its OS buffer.
    """
    state: dict = {"payload": None, "last_assistant_usage": None,
                   # Phase timing. `t0` starts before Popen so `startup`
                   # includes process spawn, not just the CLI's own init.
                   "t0": time.monotonic(), "mark": None, "saw_init": False,
                   "phases": [], "pending_tools": {}, "round_trips": 0}
    out_lines: list[str] = []
    err_lines: list[str] = []
    proc = None
    timeout_reason = f"timed out after {timeout}s"
    cancelled = threading.Event()
    steer_queue: queue.Queue = queue.Queue()

    def _write_user_message(text: str) -> None:
        write(proc, text)
        if submitted is not None:
            submitted.append(text)

    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd), env=config.agent_env(),
                                stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
        if cancel_key:
            with _active_lock:
                _active[cancel_key] = {"proc": proc, "cancelled": cancelled,
                                        "steer_queue": steer_queue,
                                        "accepting_steers": steerable, "progress": ""}

        _write_user_message(prompt)

        def _drain_stderr() -> None:
            for line in proc.stderr:
                err_lines.append(line)

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        # Two deadlines, not one. `timeout` used to be a plain wall-clock cap
        # on the entire run, which killed a session for *taking long* rather
        # than for being *stuck* -- on 2026-09-08 it ended an iOS app's
        # build 24 minutes in, at 74 steps, while it was still emitting a tool
        # call every ~20 seconds. Nothing was wrong with that run except the
        # clock. What actually tells a hung process apart from a busy one is
        # whether anything is still coming out of it, so the deadline that
        # normally fires is the *idle* one, reset on every line of stream.
        # `timeout` stays as an absolute backstop, since a run that keeps
        # emitting forever is a runaway loop and still needs an end.
        # A ceiling of 0 (or none) means no absolute limit: the idle deadline
        # alone decides, for a user who would rather a long run finish than be
        # cut off at an arbitrary hour.
        hard_deadline = time.monotonic() + timeout if timeout else float("inf")
        idle_deadline = time.monotonic() + idle_timeout
        while True:
            now = time.monotonic()
            if now >= hard_deadline:
                timeout_reason = f"hit its {timeout}s ceiling"
                raise subprocess.TimeoutExpired(cmd, timeout)
            if now >= idle_deadline:
                # Silence, not slowness. A long single tool call (a full
                # xcodebuild, a big rsync) emits nothing while it runs, which
                # is why this is generous rather than tight.
                timeout_reason = f"went silent for {idle_timeout}s"
                raise subprocess.TimeoutExpired(cmd, idle_timeout)
            remaining = hard_deadline - now if timeout else 0.5

            # Forward anything steer() queued, every iteration -- this is what
            # makes it *mid-turn*: it doesn't wait for a result event, it
            # writes the moment something is waiting, same as a person typing
            # into the Claude Code TUI mid-response. (Only ever non-empty for
            # a steerable engine; steer() refuses the others up front.)
            while not steer_queue.empty():
                _write_user_message(steer_queue.get_nowait())

            # Poll in short slices rather than blocking for the full timeout,
            # so a steer queued between reads doesn't sit until the next
            # stdout line happens to arrive.
            ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 0.5))
            if not ready:
                continue
            line = proc.stdout.readline()
            if line == "":
                break  # EOF: the engine closed stdout, so it's done writing
            idle_deadline = time.monotonic() + idle_timeout
            out_lines.append(line)
            handler(line, state, on_progress, cancel_key)

            if state["payload"] is not None:
                # Saw a result. One more check, atomic with the flag flip
                # below, for a steer that landed in this exact instant --
                # see steer()'s docstring for why this can't be two steps.
                if cancel_key:
                    with _active_lock:
                        entry = _active.get(cancel_key)
                        pending = not steer_queue.empty()
                        if entry is not None and not pending:
                            entry["accepting_steers"] = False
                else:
                    pending = not steer_queue.empty()
                if pending:
                    # Not actually final -- a fresh result is needed once the
                    # steer just queued (written at the top of the next
                    # iteration) has been absorbed.
                    state["payload"] = None
                    continue
                break

        if not proc.stdin.closed:
            proc.stdin.close()
        code = proc.wait(timeout=max(0.0, hard_deadline - time.monotonic()) + 5)
        stderr_thread.join(timeout=5)
    except subprocess.TimeoutExpired:
        if proc is not None:
            proc.kill()
            proc.wait()
        # Return the partial stream rather than "". It carries the session_id
        # of the run that just died, and that id is the only handle anything
        # has on the work it already did. Discarding it (as this used to)
        # orphaned real sessions: files left on disk, conversation
        # unreachable, nothing for the next turn to --resume.
        return (124, None, None, "".join(out_lines),
                f"{engine} {timeout_reason}", cancelled.is_set())
    finally:
        if cancel_key:
            with _active_lock:
                if _active.get(cancel_key, {}).get("proc") is proc:
                    _active.pop(cancel_key, None)
        # A timed-out or cancelled run is exactly when someone wants to know
        # where the time went, so this belongs in `finally` rather than beside
        # the success return.
        if timing is not None:
            timing["phases"] = state.get("phases") or []
            timing["round_trips"] = state.get("round_trips") or 0

    out, err = "".join(out_lines), "".join(err_lines)
    payload, context_tokens = finish(state)
    return code, payload, context_tokens, out, err, cancelled.is_set()


_ANOMALY_LOG = config.LOGS / "think-anomalies.jsonl"


def _is_anomalous(payload: dict) -> bool:
    """True if `payload` claims a clean success (is_error false, exit 0) but
    the actual result text is empty, or the cost is $0 on a run that plainly
    took more than one turn to produce.

    This is the signature of a known, currently-unresolved bug in the
    `claude` CLI itself, not something particular to Herald's use of it:
    anthropics/claude-code#38805, #38706, #38623 and #7124 all report `claude
    -p` returning `is_error: false`, exit 0, and a real session_id/duration
    while the `result` field comes back empty -- tokens generated and billed,
    payload silently discarded. Confirmed against a real incident here on
    2026-09-08: the session's own `.jsonl` transcript had the correct final
    answer, generated a couple of seconds before this exact check would have
    seen an empty "ok" -- see ledger/journal/2026-09-08.md. A standalone
    repro against three shapes of call (short/no-tools, long/multi-tool,
    resumed) did not reproduce it on demand, consistent with every upstream
    report describing it as intermittent.
    """
    text = (payload.get("result") or "").strip()
    cost = payload.get("total_cost_usd")
    turns = payload.get("num_turns") or 0
    return not text or (not cost and turns > 1)


def _log_anomaly(stage: str, label: str, model: str, resume: str | None,
                 payload: dict, out: str, err: str) -> None:
    """Append raw evidence for an empty/zero-cost 'ok' result so the next
    occurrence leaves a trail instead of needing journalctl cross-referenced
    against a session transcript by hand, the way the first one did. Every
    field here was already sitting in memory and discarded before this
    existed -- this changes nothing about what runs, only what survives it.
    Best-effort: a logging failure must never be the reason a turn fails.
    """
    try:
        config.LOGS.mkdir(parents=True, exist_ok=True)
        with _ANOMALY_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": time.time(), "stage": stage, "label": label, "model": model,
                "resume": resume, "payload": payload,
                "stdout_tail": out[-4000:], "stderr_tail": err[-2000:],
            }) + "\n")
    except OSError:
        pass


def _ok_result(payload: dict, engine: str, model: str | None,
               duration_ms: int) -> tuple[Result, dict]:
    usage = payload.get("usage") or {}
    result = Result(
        ok=True,
        text=payload.get("result", "") or "",
        engine=engine,
        model=model,
        session_id=payload.get("session_id"),
        cost_usd=payload.get("total_cost_usd"),
        duration_ms=duration_ms,
        structured=payload.get("structured_output"),
        denials=payload.get("permission_denials") or [],
    )
    return result, usage


def _session_from_stream(stream: str) -> str | None:
    """The session id of a run that died before emitting its `result` event.

    Every claude stream-json line carries `session_id`, not just the final
    one, and Codex's first line (`thread.started`) carries `thread_id`, so a
    process killed mid-tool-call still leaves its id in what it already wrote.
    That is the whole point: `result.session_id` is None on a failed run, and
    without this a timeout would throw away the only way back to the work.
    """
    for line in reversed(stream.splitlines()):
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(event, dict) and (event.get("session_id") or event.get("thread_id")):
            return event.get("session_id") or event.get("thread_id")
    return None


def _record(**fields) -> None:
    """Book a run in the ledger without ever letting the bookkeeping fail the
    run it is bookkeeping for.

    Every call site below fires *after* the model has already done the work
    and, on the ok path, after the reply is in hand. Letting the ledger write
    raise there throws away a finished answer in order to record that it
    happened, which is the wrong way round.

    Not hypothetical: `collector.run` holds one write transaction open for the
    whole length of a collector, network fetches included, so `database is
    locked` here means "something long is ingesting", not "the ledger is
    broken". On 9 Sep 2026 that same contention, unguarded on the bridge's own
    checkpoint, took herald-telegram down seven times and cost two replies.
    Losing a `runs` row costs a line of spend attribution; losing the turn
    costs the user the answer the user is waiting on.
    """
    timing = fields.pop("timing", None) or {}
    phases = timing.get("phases") or []
    if phases:
        # Aggregates go on `runs` so the common question ("where does a turn
        # go?") is one query with no join. The per-phase rows are for the
        # follow-up question ("which tool?").
        #
        # Caveat worth knowing before trusting the sum: tools issued in one
        # assistant message run concurrently, so `tool_ms` can exceed the
        # wall clock for that span. It is "time spent in tools", not "time
        # the run was blocked on tools".
        fields["startup_ms"] = sum(p["ms"] for p in phases
                                   if p["kind"] == "startup") or None
        fields["model_ms"] = sum(p["ms"] for p in phases
                                 if p["kind"] == "model") or None
        fields["tool_ms"] = sum(p["ms"] for p in phases
                                if p["kind"] == "tool") or None
    fields["round_trips"] = timing.get("round_trips") or None
    try:
        with db.session() as con:
            run_id = db.record_run(con, **fields)
            db.record_phases(con, run_id, phases)
    except Exception as exc:  # noqa: BLE001 -- bookkeeping is never worth a turn
        print(f"herald: run not recorded ({type(exc).__name__}: {exc})",
              file=sys.stderr, flush=True)


def think(prompt: str, *, label: str, escalate: bool = False,
          model: str | None = None, cwd: Path | str | None = None,
          timeout: int | None = None, idle_timeout: int | None = None,
          allowed_tools: list[str] | None = None,
          append_system_prompt: str | None = None, json_schema: dict | None = None,
          resume: str | None = None, permission_mode: str | None = None,
          add_dirs: list[str] | None = None,
          cancel_key: str | None = None,
          on_progress=None,
          engine: str | None = None, effort: str | None = None) -> Result:
    """Run one metered agent invocation.

    `label` is what shows up in the runs table — use `cycle:dawn`,
    `collector:opportunities`, `ask`, so spend can be attributed later.

    `engine` is "claude" or "codex"; None means `engines.default_engine` from
    the config. `model` and `effort` are read as belonging to that engine:
    a model name is passed through as given, and None means the engine's
    configured model (`engines.primary.model` for claude, `engines.codex.model`
    for codex, where null defers to Codex's own config.toml). `effort` is one
    of `EFFORTS`; None means `engines.<engine>.effort`, which itself defaults
    to null -- the CLI's own default.

    `escalate` picks the engine's `escalate_model`. For Codex, whose models
    are few and whose default is already the large one, an install that sets
    no escalate model gets `engines.codex.escalate_effort` instead: the same
    model, told to reason harder.

    `on_progress` receives a `Progress` for each thing the turn does: the
    model's own narration as it writes it, and each tool call as it makes
    it. Both come from the event stream that is already flowing.

    `cancel_key`, if given, registers the underlying subprocess with
    `cancel()` under that key for exactly as long as this call is running --
    pass the same key to `cancel()` from another thread to interrupt this
    call in flight (see `bin/herald-telegram`'s `/stop`).

    There is no fallback between engines. A rate limit, a timeout or a crash
    comes back as `ok=False` with the session id attached where one exists, so
    the caller can resume into the work that did happen rather than starting
    over on top of half-finished files -- on the same engine, since a
    session id means nothing to the other one.
    """
    cwd = Path(cwd) if cwd else config.ROOT
    engine = resolve_engine(engine)
    # `timeout` is an absolute ceiling on the run; `idle_timeout` is how long
    # it may produce nothing at all before being treated as hung. The second
    # is the one that normally fires -- see the comment in _run_process.
    # 0 in the config means no ceiling at all, so only a missing argument
    # falls through to it.
    if timeout is None:
        timeout = config.get("engines.think_timeout_seconds", 7200)
    idle_timeout = idle_timeout or config.get("engines.think_idle_timeout_seconds", 900)
    # The claude block is still called `primary` in config for the sake of
    # every config.json that already sets `engines.primary.model`.
    block = "engines.primary" if engine == "claude" else "engines.codex"
    effort = normalize_effort(effort) or normalize_effort(config.get(f"{block}.effort"))
    if model is None:
        if escalate:
            model = config.get(f"{block}.escalate_model")
            if model is None and engine == "codex":
                effort = normalize_effort(config.get(f"{block}.escalate_effort", "xhigh"))
        if model is None:
            model = config.get(f"{block}.model", "sonnet" if engine == "claude" else None)
    permission_mode = permission_mode or config.get("engines.primary.permission_mode", "auto")

    def _launch(text: str, resume_id: str | None, timing: dict):
        if engine == "codex":
            return _run_codex(
                text, model=model, effort=codex_effort(effort), cwd=cwd,
                timeout=timeout, idle_timeout=idle_timeout,
                append_system_prompt=append_system_prompt, json_schema=json_schema,
                resume=resume_id, permission_mode=permission_mode, add_dirs=add_dirs,
                on_progress=on_progress, cancel_key=cancel_key, timing=timing)
        return _run_claude(
            text, model=model, effort=claude_effort(effort), cwd=cwd,
            timeout=timeout, idle_timeout=idle_timeout, allowed_tools=allowed_tools,
            append_system_prompt=append_system_prompt, json_schema=json_schema,
            resume=resume_id, permission_mode=permission_mode, add_dirs=add_dirs,
            on_progress=on_progress, cancel_key=cancel_key, timing=timing)

    started = time.monotonic()
    timing: dict = {}
    code, payload, context_tokens, out, stderr, cancelled = _launch(prompt, resume, timing)
    elapsed = int((time.monotonic() - started) * 1000)

    if cancelled:
        # A deliberate interruption, not a failure, and worth telling apart
        # from "it broke" so a caller can say something calmer than
        # "That failed: exit -15".
        _record(timing=timing, label=label, engine=engine, model=model,
                duration_ms=elapsed, exit_code=code,
                error="cancelled by user")
        return Result(ok=False, engine=engine, model=model, duration_ms=elapsed,
                      error="cancelled by user", cancelled=True)

    # The empty-result anomaly is a claude-code CLI bug (see _is_anomalous);
    # Codex reports no cost at all, so its zero-cost half would fire on every
    # Codex run, and an empty final message from Codex is simply an empty
    # answer.
    anomalous = engine == "claude" and payload is not None and _is_anomalous(payload)

    if payload and not payload.get("is_error") and code == 0 and not anomalous:
        result, usage = _ok_result(payload, engine, model, payload.get("duration_ms", elapsed))
        _record(timing=timing, label=label, engine=engine, model=model,
                session_id=result.session_id, duration_ms=result.duration_ms,
                cost_usd=result.cost_usd,
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                cache_read=usage.get("cache_read_input_tokens"),
                cache_write=usage.get("cache_creation_input_tokens"),
                num_turns=payload.get("num_turns"), context_tokens=context_tokens,
                exit_code=0)
        return result

    if payload and not payload.get("is_error") and code == 0 and anomalous:
        # A clean-looking success with the payload itself empty or free --
        # see _is_anomalous's docstring. Never let this become a silent
        # "(no reply)" to whoever is waiting on the other end: log the raw
        # evidence, then retry once by asking the same session to resend
        # its last answer, the same way herald-telegram already retries once
        # on a stale --resume id rather than failing outright.
        _log_anomaly("first", label, model, resume, payload, out, stderr)
        _record(timing=timing, label=label, engine=engine, model=model,
                session_id=payload.get("session_id"), duration_ms=elapsed,
                num_turns=payload.get("num_turns"), context_tokens=context_tokens,
                exit_code=0,
                error="empty/zero-cost result on an ok run, retrying")

        retry_session = payload.get("session_id")
        if not retry_session:
            # Nothing to resume against -- can't ask it to resend anything.
            # By every other signal (is_error false, exit 0) this succeeded,
            # so return it rather than inventing a failure; the anomaly is
            # at least on record now instead of vanishing unnoticed.
            result, _ = _ok_result(payload, engine, model, payload.get("duration_ms", elapsed))
            return result

        nudge = ("Your last reply to my previous message did not reach me -- "
                 "a CLI transport issue lost it before I saw it. Resend the "
                 "final answer to that message now, in full. Do not repeat, "
                 "redo, or re-run any actions; only restate the text you "
                 "already produced.")
        r_started = time.monotonic()
        r_code, r_payload, r_ctx, r_out, r_err, r_cancelled = _launch(
            nudge, retry_session, timing)
        r_elapsed = int((time.monotonic() - r_started) * 1000)

        if r_cancelled:
            _record(timing=timing, label=label, engine=engine, model=model,
                    duration_ms=r_elapsed, exit_code=r_code,
                    error="cancelled by user (during empty-result retry)")
            return Result(ok=False, engine=engine, model=model,
                          duration_ms=elapsed + r_elapsed,
                          error="cancelled by user", cancelled=True)

        if r_payload and not r_payload.get("is_error") and r_code == 0 \
                and not _is_anomalous(r_payload):
            result, usage = _ok_result(r_payload, engine, model, elapsed + r_elapsed)
            _record(timing=timing, label=label, engine=engine, model=model,
                    session_id=result.session_id, duration_ms=r_elapsed,
                    cost_usd=result.cost_usd,
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    cache_read=usage.get("cache_read_input_tokens"),
                    cache_write=usage.get("cache_creation_input_tokens"),
                    num_turns=r_payload.get("num_turns"), context_tokens=r_ctx,
                    exit_code=0,
                    error="recovered after empty-result retry")
            return result

        # The retry didn't recover it either. Log it and fail honestly rather
        # than returning a second silent empty "ok" -- an explicit error at
        # least tells the caller (and, via send()'s fallback, the user) that
        # something is wrong, instead of reading as a turn that simply had
        # nothing to say.
        _log_anomaly("retry", label, model, retry_session, r_payload or {}, r_out, r_err)
        _record(timing=timing, label=label, engine=engine, model=model,
                duration_ms=r_elapsed, exit_code=r_code,
                error="retry also came back empty/anomalous")
        return Result(
            ok=False, engine=engine, model=model, duration_ms=elapsed + r_elapsed,
            session_id=retry_session,
            error=("the reply was lost to a claude-code CLI bug -- an empty "
                   "result field on an otherwise-successful turn (known "
                   "upstream issue, e.g. anthropics/claude-code#38805) -- and "
                   "a retry did not recover it. Any real work this turn did "
                   "should still have happened; check the session transcript "
                   "or git log directly."))

    engine_error = (stderr or "").strip() or (payload or {}).get("result") or f"exit {code}"
    limited = _looks_rate_limited(code, payload, stderr)
    if limited:
        engine_error = f"{engine.capitalize()} is rate limited or out of quota: {engine_error}"

    _record(timing=timing, label=label, engine=engine, model=model,
            duration_ms=elapsed, exit_code=code,
            error=engine_error[:2000])
    # Carry the session id even though this failed. A timeout kills the
    # process, not the transcript -- the hours of work it did are still
    # there, and this is what lets the next turn resume into it instead
    # of starting over on top of half-finished files.
    return Result(ok=False, engine=engine, model=model, duration_ms=elapsed,
                  session_id=_session_from_stream(out) or resume,
                  rate_limited=limited, error=engine_error)
