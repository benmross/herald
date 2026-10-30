"""Auto: which provider a Telegram topic continues on when its own is at a limit.

The default rule is that a failure stays a failure. Auto is the exception an
install opts into at setup: a topic keeps its provider while that works, and
moves only at a turn boundary, for a verified quota limit, to another provider
the install enabled. It reads cached quota and never makes a model call to
decide. It buys nothing: a provider at its limit is left, not topped up. Native
sessions do not cross providers, so a move always starts a new session from the
topic's saved work. Engines still start only from `think.py`.
"""
from __future__ import annotations

import datetime as dt
import json

from . import config, db, usage


def enabled(ts: dict) -> bool:
    return (bool(config.get("engines.auto.enabled", False)) and not ts.get("pinned")
            and len(config.enabled_engines()) > 1)


def _limit_until(con, engine: str, now: float) -> float | None:
    """When a saved reading says this provider's limit lifts, if it is at one."""
    row = con.execute("SELECT data FROM facts WHERE source=? AND kind='snapshot' AND external_id=?",
                      (usage.SOURCE, engine)).fetchone()
    if not row:
        return None
    snapshot = json.loads(row[0])
    if now - snapshot["sampled_at"] > float(config.get("usage.max_age_minutes", 45)) * 60:
        return None
    limit = float(config.get("engines.auto.limit_percent", 99))
    resets = [w["resets_at"] for w in snapshot["windows"]
              if w["resets_at"] and w["resets_at"] > now and w["used_percent"] >= limit]
    return max(resets) if resets else None


def alternative(con, engine: str) -> str | None:
    now = dt.datetime.now(config.tz()).timestamp()
    return next((e for e in config.enabled_engines()
                 if e != engine and _limit_until(con, e, now) is None), None)


def before_turn(con, engine: str) -> tuple[str, float] | None:
    """(Provider to move to, when the limit lifts), or None to stay."""
    until = _limit_until(con, engine, dt.datetime.now(config.tz()).timestamp())
    other = alternative(con, engine) if until else None
    return (other, until) if other else None


def record(con, key: str, source: str, target: str, why: str) -> None:
    db.put_fact(con, usage.SOURCE, "route", external_id=f"{key}:{db.now()}", ts=db.now(),
                data={"topic": key, "from": source, "to": target, "why": why})
    con.commit()
