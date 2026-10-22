"""Subscription quotas, sampled without a model call.

CLI transports live in think.py. This module strictly interprets their data,
keeps the last successful reading separate from errors, and sends deterministic
reminders. A deadline is evidence of a date, not proof work is unfinished: only
open obligations are described as unfinished. Upcoming deadlines are worded as
dates on the calendar. No tokens-to-quota or dollars-to-quota conversion exists.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import math
import re
import time
from zoneinfo import ZoneInfo

from . import config, db, notify, think

SOURCE = "subscription_usage"


class UsageError(RuntimeError):
    pass


class UsageParseError(UsageError):
    pass


def _error(detail: str) -> UsageParseError:
    return UsageParseError("Cannot parse Claude /usage pane: " + detail +
                           ". The CLI layout may have changed; update the parser. "
                           "No fresh usage reading was saved.")


def _excerpt(block: str) -> str:
    """The quota lines of one section, for an error message. Only text from
    inside a quota section reaches here, never the pane's startup output."""
    text = " / ".join(line.strip(" █▌▏▎▍▋▊▉░▒▓") for line in block.splitlines() if line.strip(" █▌▏▎▍▋▊▉░▒▓"))
    return text[:160] or "nothing"


def _reset(text: str, now: dt.datetime) -> int:
    """Known CLI reset formats only. Refuse ambiguous or changed formats."""
    text = text.strip()
    zone = re.search(r"\s*\(([^)]+)\)$", text)
    if zone:
        try:
            now = now.astimezone(ZoneInfo(zone[1]))
        except (KeyError, ValueError):
            raise _error("unrecognised reset timezone") from None
        text = text[:zone.start()].strip()
    if text.startswith("in "):
        parts = re.findall(r"(\d+)\s*(h(?:ours?)?|m(?:in(?:utes?)?)?|s(?:ec(?:onds?)?)?)", text[3:])
        remainder = re.sub(r"\d+\s*(?:hours?|h|minutes?|mins?|m|seconds?|secs?|s)", "", text[3:]).strip()
        if parts and not remainder:
            seconds = sum(int(n) * {"h": 3600, "m": 60, "s": 1}[unit[0]] for n, unit in parts)
            return int(now.timestamp() + seconds)
    text = re.sub(r"\bat\s+", "", text, flags=re.I)
    text = re.sub(r"\s+(am|pm)\b", r"\1", text, flags=re.I)
    for fmt in ("%I:%M%p", "%I%p", "%H:%M"):
        try:
            clock = dt.datetime.strptime(text, fmt).time()
        except ValueError:
            continue
        result = dt.datetime.combine(now.date(), clock, now.tzinfo)
        if result <= now:
            result += dt.timedelta(days=1)
        return int(result.timestamp())
    for fmt in ("%b %d, %I:%M%p", "%b %d, %I%p", "%b %d %I:%M%p", "%b %d %I%p"):
        try:
            result = dt.datetime.strptime(f"{now.year} {text}", "%Y " + fmt).replace(tzinfo=now.tzinfo)
        except ValueError:
            continue
        if result < now - dt.timedelta(minutes=2):
            if now.month == 12 and result.month == 1:
                result = result.replace(year=result.year + 1)
            else:
                raise _error("reset date is in the past")
        return int(result.timestamp())
    for fmt in ("%a %I:%M%p", "%a %I%p"):
        try:
            parsed = dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        result = dt.datetime.combine(now.date(), parsed.time(), now.tzinfo)
        weekday = time.strptime(text, fmt).tm_wday
        result += dt.timedelta(days=(weekday - now.weekday()) % 7)
        if result <= now:
            result += dt.timedelta(days=7)
        return int(result.timestamp())
    raise _error("unrecognised reset time")


def parse_claude(screen: str, now: dt.datetime | None = None) -> list[dict]:
    now = now or dt.datetime.now(config.tz())
    if "showing last-known usage" in screen.lower():
        raise UsageError("Claude /usage is showing last-known usage, not a fresh reading. Retry later.")
    if re.search(r"(?:usage endpoint|usage request).*rate limited|failed to (?:load|fetch).*usage",
                 screen, re.I):
        raise UsageError("Claude /usage could not fetch fresh limits. Retry later.")
    # Match sections, never the unrelated percentages in activity breakdowns.
    headings = list(re.finditer(r"^\s*(Current [^\n]+)\s*$", screen, re.M))
    windows = []
    for i, heading in enumerate(headings):
        label = heading[1].strip()
        if label == "Current session":
            key, minutes = "session", 300
        elif label in ("Current week", "Current week (all models)"):
            key, minutes = "week", 10080
        elif re.fullmatch(r"Current week \([A-Za-z0-9 .-]+ only\)", label):
            key, minutes = re.sub(r"\W+", "_", label.lower()).strip("_"), 10080
        else:
            raise _error("unrecognised quota heading")
        block = screen[heading.end():headings[i + 1].start() if i + 1 < len(headings) else len(screen)]
        # The final section is followed by unrelated usage percentages.
        block = re.split(r"\n\s*(?:What's contributing|Usage credits|Extra usage|Esc to cancel)", block)[0]
        values = re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)%\s+(used|remaining)\b", block)
        resets = re.findall(r"^\s*Resets\s+([^\n]+)", block, re.M)
        if (key == "session" and not resets
                and (not values or values == [("0", "used")] or values == [("100", "remaining")])):
            # The five-hour window starts at the first message after the last
            # one lapsed, so in between there is nothing to reset. The check
            # runs every half hour and kept landing in that gap.
            windows.append({"key": key, "label": label, "used_percent": 0.0,
                            "window_minutes": minutes, "resets_at": None})
            continue
        if len(values) != 1 or len(resets) != 1:
            raise _error(f"expected one percentage and reset time for {label} "
                         f"(saw: {_excerpt(block)})")
        percentage = float(values[0][0])
        if not 0 <= percentage <= 100:
            raise _error("percentage outside 0–100")
        if values[0][1] == "remaining":
            percentage = 100 - percentage
        reset = _reset(resets[0], now)
        if not now.timestamp() < reset <= now.timestamp() + (minutes + 2) * 60:
            raise _error("reset time falls outside its quota window")
        windows.append({"key": key, "label": label, "used_percent": percentage,
                        "window_minutes": minutes, "resets_at": reset})
    keys = [w["key"] for w in windows]
    if "session" not in keys or "week" not in keys or len(keys) != len(set(keys)):
        raise _error("expected unique current-session and current-week quotas")
    return windows


def parse_codex(payload: dict) -> list[dict]:
    buckets = payload.get("rateLimitsByLimitId")
    if buckets is None:
        buckets = {"codex": payload.get("rateLimits")}
    if not isinstance(buckets, dict) or not buckets:
        raise UsageParseError("Codex returned no subscription quota buckets")
    windows = []
    for bucket, data in buckets.items():
        if not isinstance(data, dict):
            raise UsageParseError("Codex returned an invalid subscription quota bucket")
        for name in ("primary", "secondary"):
            value = data.get(name)
            if value is None:
                continue
            try:
                used, minutes, reset = value["usedPercent"], value["windowDurationMins"], value["resetsAt"]
                if (any(isinstance(v, bool) or not isinstance(v, (int, float))
                        or not math.isfinite(v) for v in (used, minutes, reset))
                        or not 0 <= used <= 100 or minutes <= 0 or reset <= 0):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise UsageParseError("Codex returned invalid quota percentage or reset fields") from None
            windows.append({"key": f"{bucket}:{name}", "label": f"{bucket} {minutes / 60:g}h",
                            "used_percent": used, "window_minutes": minutes, "resets_at": int(reset)})
    if not windows:
        raise UsageParseError("Codex returned no subscription quota windows; check its login/plan")
    return windows


def _saved(con, kind: str, key: str) -> dict | None:
    row = con.execute("SELECT data FROM facts WHERE source=? AND kind=? AND external_id=?",
                      (SOURCE, kind, key)).fetchone()
    return json.loads(row[0]) if row else None


def _save(con, kind: str, key: str, value: dict):
    db.put_fact(con, SOURCE, kind, external_id=key, ts=db.now(), data=value)


def refresh(con, engine: str) -> dict:
    if engine not in config.enabled_engines():
        raise UsageError(f"{engine} is not enabled")
    folder = config.HOME / "run"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"usage-{engine}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UsageError(f"{engine} usage check is already running; retry shortly") from None
        try:
            raw = think.subscription_usage(engine)
            windows = parse_claude(raw) if engine == "claude" else parse_codex(raw)
            now = dt.datetime.now(config.tz()).timestamp()
            if any(w["resets_at"] is not None and w["resets_at"] <= now for w in windows):
                raise UsageError(f"{engine} returned expired quota windows; no fresh reading saved")
            snapshot = {"engine": engine, "sampled_at": now, "windows": windows}
            _save(con, "snapshot", engine, snapshot)
            _save(con, "status", engine, {"ok": True, "checked_at": now})
            con.commit()
            return snapshot
        except Exception as exc:
            # Never persist raw CLI output or server errors with login data.
            message = str(exc) if isinstance(exc, (UsageError, RuntimeError)) else f"{type(exc).__name__} during usage check"
            _save(con, "status", engine, {"ok": False, "checked_at": dt.datetime.now().timestamp(),
                                          "error": message})
            con.commit()
            raise UsageError(message) from None


def _due(value: str) -> dt.datetime | None:
    try:
        result = (dt.datetime.combine(dt.date.fromisoformat(value), dt.time.max)
                  if len(value) == 10 else dt.datetime.fromisoformat(value.replace("Z", "+00:00")))
        return result.replace(tzinfo=config.tz()) if result.tzinfo is None else result
    except (ValueError, TypeError):
        return None


def upcoming_work(con, now: dt.datetime) -> list[dict]:
    """Use recorded open loops and dates, without asserting submission state."""
    horizon = now + dt.timedelta(hours=float(config.get("usage.work_horizon_hours", 24)))
    rows = con.execute("SELECT text title, due, 'open obligation' evidence FROM commitments "
                       "WHERE status='open' AND due IS NOT NULL UNION ALL "
                       "SELECT title, due, 'calendar deadline' evidence FROM deadlines "
                       "WHERE status='live' AND kind IN ('due','quiz','test') "
                       "AND substr(due,1,10) >= ?", (now.date().isoformat(),)).fetchall()
    result = []
    for row in rows:
        when = _due(row["due"])
        if when and now <= when <= horizon:
            result.append({"title": row["title"], "due": when, "evidence": row["evidence"],
                           "date_only": len(row["due"]) == 10})
    return sorted(result, key=lambda r: r["due"])


def reminders(con, snapshot: dict) -> int:
    """One alert per threshold and reset window. No inference or task blocking."""
    now = dt.datetime.now(config.tz())
    if now.timestamp() - snapshot["sampled_at"] > float(config.get("usage.max_age_minutes", 45)) * 60:
        return 0
    work = upcoming_work(con, now)
    lead = dt.timedelta(hours=float(config.get("usage.work_lead_hours", 2)))
    regular = config.get("usage.thresholds", [80, 95])
    early = float(config.get("usage.work_threshold", 60))
    sent = 0
    for window in snapshot["windows"]:
        if window["resets_at"] is None:
            continue
        reset = dt.datetime.fromtimestamp(window["resets_at"], config.tz())
        if reset <= now:
            continue
        relevant = [w for w in work if w["due"] - lead <= reset]
        thresholds = [*regular, *([early] if relevant else [])]
        crossed = sorted({float(t) for t in thresholds if window["used_percent"] >= float(t)})
        key = f"{snapshot['engine']}:{window['key']}:{window['resets_at']}"
        prior = _saved(con, "alert", key) or {"threshold": -1}
        if not crossed or crossed[-1] <= prior["threshold"]:
            continue
        body = (f"You have used {window['used_percent']:g}% of your {snapshot['engine'].capitalize()} "
                f"allowance ({window['label']}). It resets {reset.strftime('%a %d %b')} "
                f"at {config.clock(reset)}.")
        if relevant:
            item = relevant[0]
            prefix = "Your calendar shows" if item["evidence"] == "calendar deadline" else "You still have"
            when = item['due'].strftime('%a %d %b')
            if not item['date_only']:
                when += f" at {config.clock(item['due'])}"
            body += f" {prefix} {item['title']} due {when}."
        if notify.tell("Usage reminder", body) != "nowhere":
            _save(con, "alert", key, {"threshold": crossed[-1]})
            con.commit()
            sent += 1
    return sent


def collect(con) -> dict:
    if not config.get("usage.enabled", False):
        return {}
    counts = {"readings": 0, "reminders": 0}
    errors = []
    for engine in config.enabled_engines():
        previous = _saved(con, "status", engine)
        try:
            snapshot = refresh(con, engine)
            counts["readings"] += 1
            counts["reminders"] += reminders(con, snapshot)
        except UsageError as exc:
            errors.append(f"{engine}: {exc}")
            # A changed screen is reported on its first failure, then deduped.
            if not previous or previous.get("ok") or previous.get("error") != str(exc):
                notify.tell(f"{engine.capitalize()} usage check failed", str(exc))
    if errors:
        raise UsageError("; ".join(errors))
    return counts


def report(con, engines=None, *, force: bool = False, cached_only: bool = False) -> tuple[str, bool]:
    lines, failed = [], False
    for engine in engines or config.enabled_engines():
        now = dt.datetime.now(config.tz()).timestamp()
        if engine not in config.enabled_engines():
            lines.append(f"{engine}: ERROR: provider is not enabled")
            failed = True
            continue
        snapshot = _saved(con, "snapshot", engine)
        status = _saved(con, "status", engine)
        if not cached_only and (force or not snapshot or not status or not status.get("ok")
                                or now - snapshot["sampled_at"] > 300):
            try:
                snapshot = refresh(con, engine)
            except UsageError as exc:
                lines.append(f"{engine}: ERROR: {exc}")
                failed = True
            status = _saved(con, "status", engine)
        now = dt.datetime.now(config.tz()).timestamp()
        if status and not status["ok"]:
            message = f"{engine}: ERROR: {status['error']}"
            if message not in lines:
                lines.append(message)
            failed = True
        if not snapshot:
            lines.append(f"{engine}: no usage reading available")
            failed = True
            continue
        age = max(0, int((now - snapshot["sampled_at"]) / 60))
        stale = age > float(config.get("usage.max_age_minutes", 45)) or any(
            w["resets_at"] is not None and w["resets_at"] <= now for w in snapshot["windows"])
        lines.append(f"{engine} ({'STALE, ' if stale or status and not status['ok'] else ''}sampled {age}m ago)")
        for window in snapshot["windows"]:
            if window["resets_at"] is None:
                lines.append(f"  {window['label']}: not started; begins with the next message")
                continue
            reset = dt.datetime.fromtimestamp(window["resets_at"], config.tz())
            lines.append(f"  {window['label']}: {window['used_percent']:g}% used; "
                         f"resets {reset.strftime('%a %d %b')} {config.clock(reset)}")
        failed = failed or stale
    return "\n".join(lines), failed
