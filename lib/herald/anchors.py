"""Keep each subscription's five-hour window on a schedule the person benefits from.

A five-hour window opens with the first message after the last one lapsed, so
when it opens decides when its allowance comes back. Left alone, that is
whenever a 6:30 AM digest happened to run. This module opens the windows on
purpose, with a one-word call, so that the windows covering the person's usual
start are nearly empty, a reset is always within a few hours, and two providers
reset about two and a half hours apart.

Five-hour windows do not fit a 24-hour day: five of them make 25 hours. Each
engine runs four a day and sits idle for four hours. Anything that runs in that
gap opens a window off schedule, so the plan is a search over the day for the
placement that history says leaves the gap empty and the start of the day
clean. The plan is arithmetic over `runs`. Calls are spent only when a window
is idle and one is due, never without enough history, and never for someone who
has stopped using Herald.

`usage.anchors.enabled: false` turns it off.
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
import tempfile
from pathlib import Path

from . import config, db, think

SOURCE = "subscription_usage"
LABEL = "usage:anchor"
DAY = 1440
SLOT = 30
WINDOW = 300
WINDOWS_A_DAY = 4
OFFSET = 150
LATE = 60                # a ping this far behind schedule still counts as on it
HISTORY_DAYS = 42
REPLAN_DAYS = 7
INTERACTIVE = ("telegram", "ask")
# Costs per average run a day. A run in the gap breaks the schedule until the
# next day, which is worse than one run spending a little of the window.
GAP_COST = 20
USED_COST = 5


def enabled() -> bool:
    return bool(config.get("usage.anchors.enabled", True))


def history(con, now: dt.datetime) -> list[dict]:
    since = (now - dt.timedelta(days=HISTORY_DAYS)).isoformat()
    rows = con.execute("SELECT ts,label,engine FROM runs WHERE ts >= ? AND label != ? ORDER BY ts",
                       (since, LABEL)).fetchall()
    result = []
    for row in rows:
        try:
            when = dt.datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
        except ValueError:
            continue
        when = when.astimezone(now.tzinfo)
        result.append({"when": when, "engine": row["engine"],
                       "minute": when.hour * 60 + when.minute,
                       "interactive": row["label"].split(":")[0] in INTERACTIVE})
    return result


def _in(minute: int, start: int, length: int) -> bool:
    return (minute - start) % DAY < length


def _quietest(minutes: list[int]) -> int:
    """Where one person's day ends: the emptiest four hours, not midnight."""
    return min(range(0, DAY, SLOT), key=lambda s: sum(_in(m, s, 240) for m in minutes))


def usual_start(rows: list[dict]) -> tuple[int | None, int]:
    """(Median first use of the day, rounded down to a half hour; days seen)."""
    used = [r for r in rows if r["interactive"]]
    if not used:
        return None, 0
    boundary = _quietest([r["minute"] for r in used])
    first: dict[dt.date, int] = {}
    for r in used:
        day = (r["when"] - dt.timedelta(minutes=boundary)).date()
        first.setdefault(day, (r["minute"] - boundary) % DAY)
    middle = int(statistics.median(first.values()))
    return (boundary + middle) // SLOT * SLOT % DAY, len(first)


def _cost(runs: list[int], days: int, first: int, start: int) -> float:
    awake = WINDOWS_A_DAY * WINDOW
    if _in(start, first + awake, DAY - awake):
        return float("inf")
    gap = sum(_in(m, first + awake, DAY - awake) for m in runs)
    since_open = (start - first) % WINDOW
    used = sum(_in(m, start - since_open, since_open) for m in runs)
    return (GAP_COST * gap + USED_COST * used) / days


def build(rows: list[dict], engines: tuple[str, ...], primary: str) -> dict | None:
    start, days = usual_start(rows)
    if start is None or days < int(config.get("usage.anchors.min_days", 7)):
        return None
    others = [e for e in engines if e != primary][:1]
    per = {e: [r["minute"] for r in rows if r["engine"] == e] for e in engines}

    def score(first: int) -> tuple[float, int]:
        cost = _cost(per[primary], days, first, start)
        for other in others:
            cost += _cost(per[other], days, (first + OFFSET) % DAY, start)
        # Among equals, the provider used most gets its refill soonest.
        return cost, (first - start) % WINDOW

    best = min(range(0, DAY, SLOT), key=score)
    anchors = {primary: [(best + WINDOW * i) % DAY for i in range(WINDOWS_A_DAY)]}
    for other in others:
        anchors[other] = [(best + OFFSET + WINDOW * i) % DAY for i in range(WINDOWS_A_DAY)]
    return {"start": start, "days": days, "anchors": anchors}


def current(con, now: dt.datetime | None = None) -> dict | None:
    """This week's plan. Replanned rarely: a schedule that moves never settles."""
    now = now or dt.datetime.now(config.tz())
    engines = tuple(config.enabled_engines())
    saved = _saved(con, "anchor_plan")
    if (saved and sorted(saved["anchors"]) == sorted(engines[:2])
            and now.timestamp() - saved["made_at"] < REPLAN_DAYS * 86400):
        return saved
    default = config.get("engines.default_engine")
    plan = build(history(con, now), engines, default if default in engines else engines[0])
    if plan:
        plan["made_at"] = now.timestamp()
        db.put_fact(con, SOURCE, "anchor_plan", external_id="plan", ts=db.now(), data=plan)
        con.commit()
    return plan


def _saved(con, kind: str, key: str = "plan") -> dict | None:
    row = con.execute("SELECT data FROM facts WHERE source=? AND kind=? AND external_id=?",
                      (SOURCE, kind, key)).fetchone()
    return json.loads(row[0]) if row else None


def due(plan: dict, engine: str, now: dt.datetime) -> int | None:
    minute = now.hour * 60 + now.minute
    late = [a for a in plan["anchors"].get(engine, []) if (minute - a) % DAY <= LATE]
    return late[0] if late else None


def idle(engine: str, window: dict, sampled_at: float) -> bool:
    """Whether nothing has opened this window, so one message would.

    Claude shows no reset then. Codex shows zero used and a reset that keeps
    sliding to five hours from now, seen in every idle reading of 30 Oct 2026.
    """
    if window["resets_at"] is None:
        return True
    return (engine == "codex" and window["used_percent"] == 0
            and window["resets_at"] - sampled_at >= (WINDOW - 1) * 60)


def _ping(engine: str) -> think.Result:
    model = (config.get("usage.anchors.models") or {}).get(engine)

    def call(chosen: str | None) -> think.Result:
        with tempfile.TemporaryDirectory(prefix="herald-anchor-") as empty:
            return think.think("Reply with OK.", label=LABEL, engine=engine, model=chosen,
                               effort="low", cwd=Path(empty), permission_mode="plan",
                               timeout=120, lean=True)

    result = call(model)
    # A plan may not offer the cheap model. Its usual one opens the window too.
    return call(None) if model and not result.ok and not result.rate_limited else result


def maintain(con, snapshots: dict[str, dict], plan: dict | None,
             now: dt.datetime | None = None) -> int:
    """Open each idle window whose time has come. Returns how many were."""
    if not plan:
        return 0
    now = now or dt.datetime.now(config.tz())
    last = con.execute("SELECT MAX(ts) FROM runs WHERE label LIKE 'telegram%' OR label LIKE 'ask%'"
                       ).fetchone()[0]
    idle_days = float(config.get("usage.anchors.idle_days", 7))
    if not last or (now - dt.datetime.fromisoformat(last).astimezone(now.tzinfo)
                    ).total_seconds() > idle_days * 86400:
        return 0
    opened = 0
    for engine, snapshot in snapshots.items():
        anchor = due(plan, engine, now)
        windows = snapshot["windows"]
        five = next((w for w in windows if w["window_minutes"] == WINDOW), None)
        if anchor is None or five is None or not idle(engine, five, snapshot["sampled_at"]):
            continue
        limit = float(config.get("usage.anchors.weekly_limit_percent", 90))
        if any(w["window_minutes"] > WINDOW and w["used_percent"] >= limit for w in windows):
            continue
        key = f"{(now - dt.timedelta(minutes=LATE)).date()}:{anchor}"
        state = _saved(con, "anchor_state", engine) or {}
        if state.get("last") == key:
            continue
        result = _ping(engine)
        db.put_fact(con, SOURCE, "anchor_state", external_id=engine, ts=db.now(),
                    data={"last": key, "ok": result.ok, "at": now.timestamp(),
                          "error": None if result.ok else (result.error or "")[:200]})
        con.commit()
        opened += result.ok
    return opened


def describe(con) -> str:
    plan = _saved(con, "anchor_plan")
    if not plan:
        return ""
    day = dt.datetime(2000, 1, 1, tzinfo=config.tz())
    clock = lambda m: config.clock(day + dt.timedelta(minutes=m))  # noqa: E731
    parts = [f"{engine} at {', '.join(clock(a) for a in sorted(anchors))}"
             for engine, anchors in plan["anchors"].items()]
    return (f"Windows open for a usual start of {clock(plan['start'])}: " + "; ".join(parts))
