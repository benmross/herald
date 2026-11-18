"""Cache-conscious Claude account selection. Credentials stay inside cswap.

Only think.py launches Claude. This module reads cswap's public JSON and saves
native-session ownership separately from the machine's default login.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
import subprocess
import time
from contextlib import contextmanager

from . import config


class AccountError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(config.get("engines.claude.accounts.enabled", False))


@contextmanager
def _store():
    path = config.RAW / "claude-accounts.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = None
    try:
        con = sqlite3.connect(path, timeout=15)
        con.execute("CREATE TABLE IF NOT EXISTS bindings (session TEXT PRIMARY KEY, identity TEXT NOT NULL)")
        con.execute("CREATE TABLE IF NOT EXISTS readings (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT, sampled REAL)")
        con.execute("CREATE TABLE IF NOT EXISTS moves (id INTEGER PRIMARY KEY, session TEXT NOT NULL, at REAL NOT NULL, previous TEXT NOT NULL, target TEXT NOT NULL, source_pressure REAL, target_pressure REAL)")
        yield con
        con.commit()
    except sqlite3.Error as exc:
        raise AccountError("Cannot read or save Claude session account ownership") from exc
    finally:
        if con is not None:
            con.close()


def binding(session: str | None) -> dict | None:
    if not session:
        return None
    with _store() as con:
        row = con.execute("SELECT identity FROM bindings WHERE session=?", (session,)).fetchone()
    return json.loads(row[0]) if row else None


def bind(session: str, account: dict) -> None:
    identity = {k: account[k] for k in ("email", "organizationUuid")}
    with _store() as con:
        con.execute("INSERT OR IGNORE INTO bindings VALUES (?,?)", (session, json.dumps(identity)))
        saved = json.loads(con.execute("SELECT identity FROM bindings WHERE session=?", (session,)).fetchone()[0])
        if saved != identity:
            raise AccountError("Claude session already belongs to a different account")


def accounts(*, cached_only: bool = False) -> list[dict]:
    if cached_only:
        with _store() as con:
            row = con.execute("SELECT payload, sampled FROM readings WHERE id=1").fetchone()
        if not row:
            raise AccountError("No saved Claude account usage; run herald usage claude")
        payload = json.loads(row[0])
        elapsed = max(0, time.time() - row[1])
        for account in payload["accounts"]:
            account["usageAgeSeconds"] = account.get("usageAgeSeconds", float("inf")) + elapsed
    else:
        try:
            result = subprocess.run([config.get("engines.claude.accounts.cmd", "cswap"), "list", "--json"],
                                    capture_output=True, text=True, timeout=20, env=config.agent_env())
            if result.returncode:
                raise AccountError("cswap could not read account usage; check cswap list")
            payload = json.loads(result.stdout)
            if (not isinstance(payload, dict) or payload.get("schemaVersion") != 1
                    or not isinstance(payload.get("accounts"), list)):
                raise ValueError("unsupported account JSON")
            if any(not isinstance(row, dict) or not isinstance(row.get("email"), str)
                   or not row["email"] or not isinstance(row.get("organizationUuid"), str)
                   or not isinstance(row.get("number"), int) for row in payload["accounts"]):
                raise ValueError("incomplete account identity")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise AccountError("Cannot read Claude accounts; check cswap list") from exc
        with _store() as con:
            con.execute("INSERT OR REPLACE INTO readings VALUES (1,?,?)", (json.dumps(payload), time.time()))
    return payload["accounts"]


def windows(account: dict, model: str | None = None) -> list[tuple[str, dict]]:
    usage = account.get("usage") or {}
    result = [(key, usage[key]) for key in ("fiveHour", "sevenDay") if isinstance(usage.get(key), dict)]
    for window in usage.get("scoped", []):
        name = str(window.get("name", ""))
        if model is None or name.lower().replace(" only", "") in model.lower():
            result.append((name, window))
    return result


def pressure(account: dict, model: str | None = None) -> float | None:
    """Unknown, stale and reset readings cannot become available capacity."""
    try:
        age = float(account.get("usageAgeSeconds", float("inf")))
        if (account.get("disabled") or account.get("usageStatus") != "ok"
                or not isinstance(account.get("plan"), dict)
                or account["plan"].get("expired") is not False
                or account["plan"].get("label", "").lower() in ("free", "api key")
                or not math.isfinite(age) or age > 900 or age < 0):
            return None
        values = []
        for _, window in windows(account, model):
            pct = float(window["pct"])
            if not math.isfinite(pct) or not 0 <= pct <= 100:
                return None
            reset = window.get("resetsAt")
            if not reset and pct != 0:
                return None
            if reset and dt.datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp() <= time.time():
                return None
            values.append(pct)
        if not values or not isinstance((account.get("usage") or {}).get("sevenDay"), dict):
            return None
        return max(values)
    except (KeyError, ValueError, TypeError, OverflowError):
        return None


def select(resume: str | None, model: str | None, *, allow_move: bool = False) -> dict:
    rows = accounts()

    def target(row):
        if sum(candidate.get("email") == row.get("email") for candidate in rows) != 1:
            raise AccountError("Claude account email is ambiguous in cswap; use distinct subscription logins")
        return row

    saved = binding(resume)
    owner = None
    if saved:
        matches = [row for row in rows if all(row.get(k) == v for k, v in saved.items())]
        if len(matches) != 1:
            raise AccountError("This conversation's Claude account is missing or ambiguous in cswap; restore it before resuming")
        owner = target(matches[0])
    active = next((row for row in rows if row.get("active")), None)
    if resume and owner is None:
        # Conversations predating this integration used the default login.
        if not active:
            raise AccountError("Cannot identify the default Claude account for this existing conversation")
        bind(resume, active)
        owner = target(active)
    candidates = [(pressure(row, model), row) for row in rows]
    candidates = [(pct, row) for pct, row in candidates if pct is not None and pct < 100]
    threshold = float(config.get("engines.claude.accounts.switch_threshold", 90))
    margin = float(config.get("engines.claude.accounts.switch_margin", 20))
    cooldown = float(config.get("engines.claude.accounts.cooldown_seconds", 1800))
    if not 0 < threshold <= 100:
        raise AccountError("Claude account switch threshold must be greater than 0 and at most 100")
    if not 0 < margin <= 100 or not math.isfinite(cooldown) or cooldown < 0:
        raise AccountError("Claude account switch margin must be greater than 0 and at most 100; cooldown must be nonnegative")
    if owner is not None:
        current = pressure(owner, model)
        if not allow_move or current is None or current < threshold or not candidates:
            return owner
        pct, other = min(candidates, key=lambda item: (item[0], item[1]["number"]))
        if current - pct < margin:
            return owner
        other = target(other)
        previous = {k: owner[k] for k in ("email", "organizationUuid")}
        replacement = {k: other[k] for k in previous}
        now = time.time()
        with _store() as con:
            # Check and move under one write transaction. Usage inspection
            # never opts into this path; only an actual turn launch does.
            con.execute("BEGIN IMMEDIATE")
            last = con.execute("SELECT max(at) FROM moves WHERE session=?", (resume,)).fetchone()[0]
            if last is not None and now - last < cooldown:
                return owner
            row = con.execute("SELECT identity FROM bindings WHERE session=?", (resume,)).fetchone()
            if not row or json.loads(row[0]) != previous:
                raise AccountError("Claude session account changed during selection; retry this turn")
            con.execute("UPDATE bindings SET identity=? WHERE session=?", (json.dumps(replacement), resume))
            con.execute("INSERT INTO moves(session,at,previous,target,source_pressure,target_pressure) VALUES(?,?,?,?,?,?)",
                        (resume, now, json.dumps(previous), json.dumps(replacement), current, pct))
        return dict(other, _herald_move={"previous": previous["email"], "current": current, "target": pct})
    if not candidates:
        raise AccountError("No Claude subscription has verified capacity for a new conversation; check herald usage claude")
    # New conversations have no established conversation cache to preserve.
    # Balance five-hour usage first, then weekly usage; both must have room.
    return target(min(candidates, key=lambda item: (
        float(((item[1].get("usage") or {}).get("fiveHour") or {}).get("pct", 0)),
        item[0], not item[1].get("active", False), item[1]["number"]))[1])


def report(*, cached_only: bool = False) -> tuple[str, bool]:
    rows = accounts(cached_only=cached_only)
    lines = ["Claude accounts (conversations keep their account until near quota)"]
    failed = not rows
    for row in rows:
        known = pressure(row)
        failed |= known is None
        label = row.get("alias") or row.get("email") or "Unknown account"
        lines.append(f"  {label}{' (default login)' if row.get('active') else ''}")
        if known is None:
            lines.append("    Usage unavailable, stale, disabled or subscription inactive")
            continue
        lines.append(f"    Sampled {int(row['usageAgeSeconds'] / 60)}m ago")
        for name, window in windows(row):
            reset = window.get("resetsAt")
            when = dt.datetime.fromisoformat(reset.replace("Z", "+00:00")).astimezone(config.tz()) if reset else None
            suffix = "; resets " + when.strftime("%a %d %b ") + config.clock(when) if when else ""
            label = {"fiveHour": "5-hour", "sevenDay": "Weekly"}.get(name, name)
            lines.append(f"    {label}: {window['pct']:g}% used{suffix}")
    return "\n".join(lines), failed


def snapshot(resume: str | None = None, model: str | None = None) -> dict:
    """The selected account's limits, for capacity checks and reset choices."""
    account = select(resume, model)
    if pressure(account, model) is None:
        raise AccountError("No current usage reading for this conversation's Claude account")
    result = []
    for key, window in windows(account, model):
        reset = window.get("resetsAt")
        result.append({"key": key, "label": {"fiveHour": "5-hour", "sevenDay": "Weekly"}.get(key, key),
                       "used_percent": float(window["pct"]), "window_minutes": 300 if key == "fiveHour" else 10080,
                       "resets_at": dt.datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp() if reset else None})
    return {"engine": "claude", "account": account["email"],
            "sampled_at": time.time() - account["usageAgeSeconds"], "windows": result}
