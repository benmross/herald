"""A stopped Telegram task waits for its owner's choice, without inference.

The optional topic record is additive: old topic cursors and already queued
limit_resume records keep their meaning. Nothing is scheduled by making a card.
"""
from __future__ import annotations

import math
import time
import uuid
from datetime import datetime

from . import config, limitresume, usage

KEY = "limit_choice"
CONTINUE = ("[Herald wrote this message, not the user. The user chose to continue the "
            "task stopped by a usage limit on the other engine's default model. "
            "Continue from the saved work and previous transcript. First check files, "
            "commits and the ledger to avoid repeating completed actions. Earlier "
            "approvals do not carry over.]")


def clip(text: str, size: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= size else text[:size - 1].rstrip() + "…"


def capacity(con, engine: str) -> dict | None:
    """Positive evidence of capacity, never an absent or failed reading."""
    if engine not in config.enabled_engines():
        return None
    try:
        snapshot = usage.refresh(con, engine)
        now = time.time()
        windows = snapshot["windows"]
        if not windows or not 0 <= now - snapshot["sampled_at"] <= 300:
            return None
        for window in windows:
            used, reset = window["used_percent"], window["resets_at"]
            if not math.isfinite(used) or not 0 <= used < 100:
                return None
            if reset is not None and reset <= now:
                return None
            if reset is None and used != 0:
                return None
        return snapshot
    except Exception:  # a missing reading must hide the switch, not lose the card
        return None


def clock(at: float) -> str:
    value = datetime.fromtimestamp(at, config.tz())
    return f"{value.strftime('%a %-d %b')} at {config.clock(value)}"


def make(con, ts: dict, progress: str = "", request: str = "") -> tuple[dict, str, list]:
    engine = ts["engine"]
    now = time.time()
    reset = limitresume.reset_at(con, engine, now)
    at = reset + limitresume.GRACE_SECONDS if reset else now + limitresume.RETRY_SECONDS
    other = next((e for e in config.enabled_engines() if e != engine), None)
    snapshot = capacity(con, other) if other else None
    request = request or (ts.get("work") or {}).get("request", "")
    token = uuid.uuid4().hex[:16]
    record = {"token": token, "engine": engine, "session_id": ts.get("session_id"),
              "last_ts": ts["last_ts"], "model": ts.get("model"),
              "effort": ts.get("effort"), "at": at,
              "request": clip(request, 300),
              "other": other if snapshot else None}
    lines = [f"{engine.capitalize()} hit its usage limit. This task is paused."]
    if request:
        lines.append("Task: " + clip(request, 300))
    if progress:
        lines.append("Last update: " + clip(progress, 600))
    lines.append(f"The limit resets {clock(reset)}. I'll resume just after that if you choose to wait."
                 if reset else "I couldn't verify the reset time. If you choose to wait, "
                 "I'll retry in 15 minutes.")
    buttons = []
    if snapshot:
        model = config.get(f"engines.{other}.model") or "CLI default"
        effort = config.get(f"engines.{other}.effort") or "CLI default"
        remaining = "; ".join(f"{w['label']}: {100 - w['used_percent']:g}% remaining"
                              for w in snapshot["windows"])
        lines.append(f"Switch now: {other.capitalize()} / {model}, effort {effort}. {remaining}. "
                     "It will continue from the saved work in a new session.")
        buttons.append([{"text": f"Switch to {other.capitalize()} default",
                         "callback_data": f"limit:{token}:switch"}])
    elif other:
        lines.append(f"Switch unavailable: {other.capitalize()} has no confirmed remaining usage.")
    lines.append("Nothing continues until you choose. Previous approvals do not carry over.")
    buttons += [[{"text": "Continue when limit resets", "callback_data": f"limit:{token}:wait"}],
                [{"text": "Stop", "callback_data": f"limit:{token}:stop"}]]
    return record, "\n\n".join(lines), buttons


def current(ts: dict, record: dict) -> bool:
    return (ts.get(KEY) is record and all(ts.get(k) == record.get(k)
            for k in ("engine", "session_id", "last_ts", "model", "effort")))
