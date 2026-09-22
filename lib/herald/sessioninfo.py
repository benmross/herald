"""One conversation's context and latency, from what `think.py` already records.

Built 14 Sep 2026 because the user asked from their phone for exactly this and
there was nothing to point them at. `herald latency --run` showed one turn, but
you had to find the run id first, and nothing said how big the context had
grown. Claude Code's own `/context` and `/cost` exist only in an interactive
terminal; a turn started with `claude -p` from Telegram never sees them.

Everything here comes out of `runs` and `run_phases`, so it costs no model call
and can answer while a turn in the same topic is still running. The caveat
worth repeating: a turn gets its `runs` row when it finishes, so the turn in
flight shows up as "running", not in the numbers.
"""

from __future__ import annotations

import sqlite3

TURNS_SHOWN = 8
SLOWEST_STEPS = 5


def _secs(ms) -> str:
    s = (ms or 0) / 1000
    return f"{s:.1f}s" if s < 90 else f"{int(s // 60)}m {int(s % 60)}s"


def _n(x) -> str:
    return f"{x:,}" if isinstance(x, int) else "?"


def latest_session(con: sqlite3.Connection) -> str | None:
    row = con.execute("SELECT session_id FROM runs WHERE session_id IS NOT NULL "
                      "ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def report(con: sqlite3.Connection, session_id: str | None, *,
           running: dict | None = None, model: str | None = None,
           engine: str | None = None, effort: str | None = None) -> str:
    """Plain text, a screenful. `running` is {'seconds': float, 'step': str}."""
    out: list[str] = []
    if engine or effort:
        out.append(f"engine {engine or '?'}" + (f", effort {effort}" if effort else ""))
    if running:
        step = f", now: {running['step']}" if running.get("step") else ""
        out.append(f"a turn is running: {_secs(running['seconds'] * 1000)}{step}")
        out.append("(its numbers land when it finishes)")
        out.append("")

    if not session_id:
        out.append("no session in this topic yet; the next message starts one")
        return "\n".join(out)

    runs = con.execute("""
        SELECT id, ts, label, model, duration_ms, cost_usd, input_tokens,
               output_tokens, cache_read, cache_write, context_tokens,
               startup_ms, model_ms, tool_ms, round_trips, error
        FROM runs WHERE session_id = ? ORDER BY id
    """, (session_id,)).fetchall()
    if not runs:
        out.append(f"session {session_id[:8]}: no finished turns recorded yet")
        return "\n".join(out)

    last = runs[-1]
    total_ms = sum(r["duration_ms"] or 0 for r in runs)
    cost = sum(r["cost_usd"] or 0 for r in runs)
    out.append(f"session {session_id[:8]} · {model or last['model'] or engine or '?'} · "
               f"{last['label']}")
    out.append(f"{len(runs)} turn{'' if len(runs) == 1 else 's'}, "
               f"{_secs(total_ms)} of turn time, ${cost:.2f}")
    out.append(f"context after the last turn: {_n(last['context_tokens'])} tokens")
    out.append("")

    when = (last["ts"] or "")[11:16]
    out.append(f"last turn (run {last['id']}, {when}): {_secs(last['duration_ms'])}, "
               f"{last['round_trips'] or '?'} round trips")
    out.append(f"  model {_secs(last['model_ms'])} · tools {_secs(last['tool_ms'])}"
               f" · startup {_secs(last['startup_ms'])}")
    out.append(f"  tokens: {_n(last['input_tokens'])} new in, "
               f"{_n(last['cache_read'])} cached, {_n(last['cache_write'])} "
               f"cache written, {_n(last['output_tokens'])} out")
    if last["error"]:
        out.append(f"  error: {last['error'].splitlines()[0][:200]}")

    # Thinking is summarised rather than listed: it is nearly always the bulk of
    # the time, spread across every round trip, so a top-five of it says only
    # "the model thought". The tool calls are the part worth naming.
    thought = con.execute("""
        SELECT COUNT(*) n, MAX(ms) longest FROM run_phases
        WHERE run_id = ? AND kind = 'model'
    """, (last["id"],)).fetchone()
    if thought and thought["n"]:
        out.append(f"  thinking: {thought['n']} stretches, longest "
                   f"{_secs(thought['longest'])}")
    tools = con.execute("""
        SELECT name, ms, detail FROM run_phases
        WHERE run_id = ? AND kind = 'tool' ORDER BY ms DESC LIMIT ?
    """, (last["id"], SLOWEST_STEPS)).fetchall()
    if tools:
        out.append("  slowest tool calls:")
        for t in tools:
            detail = " ".join((t["detail"] or "").split())
            # think.py's detail already opens with the tool's name.
            if detail.startswith(f"{t['name']}: "):
                detail = detail[len(t["name"]) + 2:]
            out.append(f"    {_secs(t['ms']):>6}  {t['name']}"
                       + (f"  {detail[:90]}" if detail else ""))

    if len(runs) > 1:
        out.append("")
        out.append("turns (context grows until compaction):")
        out.append("  run   at     time    trips  context")
        for r in runs[-TURNS_SHOWN:]:
            out.append(f"  {r['id']:<5} {(r['ts'] or '')[11:16]:<6} "
                       f"{_secs(r['duration_ms']):>7} {r['round_trips'] or '?':>6}  "
                       f"{_n(r['context_tokens']):>8}")
        if len(runs) > TURNS_SHOWN:
            out.append(f"  ({len(runs) - TURNS_SHOWN} earlier not shown)")
    return "\n".join(out)
