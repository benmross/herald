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
def _store(*, read_only=False):
    path = config.RAW / "claude-accounts.sqlite3"
    if read_only:
        if not path.exists():
            raise AccountError('No saved Claude account usage is available.')
        con = None
        try:
            con = sqlite3.connect(f'file:{path}?mode=ro',uri=True,timeout=15)
            yield con
        except sqlite3.Error as exc:
            raise AccountError('Saved Claude account usage is unavailable.') from exc
        finally:
            if con is not None:
                con.close()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    con = None
    try:
        con = sqlite3.connect(path, timeout=15)
        con.execute("CREATE TABLE IF NOT EXISTS bindings (session TEXT PRIMARY KEY, identity TEXT NOT NULL)")
        con.execute("CREATE TABLE IF NOT EXISTS readings (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT, sampled REAL)")
        con.execute("CREATE TABLE IF NOT EXISTS moves (id INTEGER PRIMARY KEY, session TEXT NOT NULL, at REAL NOT NULL, previous TEXT NOT NULL, target TEXT NOT NULL, source_pressure REAL, target_pressure REAL)")
        con.execute("CREATE TABLE IF NOT EXISTS reservations (token TEXT PRIMARY KEY, session TEXT NOT NULL, identity TEXT NOT NULL, budget REAL NOT NULL, expires REAL NOT NULL)")
        con.execute("CREATE TABLE IF NOT EXISTS samples (identity TEXT NOT NULL, sampled REAL NOT NULL, account TEXT NOT NULL, PRIMARY KEY(identity,sampled))")
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
        with _store(read_only=True) as con:
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
            for row in payload['accounts']:
                if row.get('usageStatus') != 'ok' or not row.get('usageFetchedAt'):
                    continue
                try:
                    sampled = dt.datetime.fromisoformat(row['usageFetchedAt'].replace('Z','+00:00')).timestamp()
                except (ValueError, TypeError):
                    continue
                identity = json.dumps({k:row[k] for k in ('email','organizationUuid')})
                con.execute('INSERT OR IGNORE INTO samples VALUES(?,?,?)',(identity,sampled,json.dumps(row)))
            con.execute('DELETE FROM samples WHERE sampled<?',(time.time()-30*86400,))
    return payload["accounts"]


def evidence(since):
    """Quota samples and numeric ownership history, never credentials."""
    with _store() as con:
        owners = {r[0]:r[1] for r in con.execute('SELECT session,identity FROM bindings')}
        moves = [tuple(r) for r in con.execute('SELECT session,at,previous,target FROM moves ORDER BY at')]
        samples = [(r[0],r[1],json.loads(r[2])) for r in con.execute('SELECT identity,sampled,account FROM samples WHERE sampled>=? ORDER BY sampled',(since,))]
    return owners,moves,samples


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


def reserved(account, *, session=None, con=None):
    identity = json.dumps({k: account[k] for k in ('email', 'organizationUuid')})
    def read(connection):
        return connection.execute('SELECT coalesce(sum(budget),0) FROM reservations WHERE identity=? AND expires>? AND session!=?',
                                  (identity, time.time(), session or '')).fetchone()[0]
    if con is not None:
        return read(con)
    with _store() as connection:
        return read(connection)


def release(token):
    if token:
        with _store() as con:
            con.execute('DELETE FROM reservations WHERE token=?', (token,))


def switch_plan(con, ts, *, hard=False):
    """Inspect an account move. No binding changes or reservations here."""
    from . import claude_budget
    threshold = float(config.get('engines.claude.accounts.switch_threshold',90))
    margin = float(config.get('engines.claude.accounts.switch_margin',20))
    cooldown = float(config.get('engines.claude.accounts.cooldown_seconds',1800))
    if not 0 < threshold <= 100 or not 0 < margin <= 100 or not math.isfinite(cooldown) or cooldown < 0:
        raise AccountError('The configured account-switch thresholds are invalid.')
    rows = accounts()
    saved = binding(ts.get('session_id'))
    source = next((r for r in rows if saved and all(r.get(k) == v for k, v in saved.items())), None)
    result = {'candidate': None, 'reason': '', 'account_count': len(rows), 'source': source}
    if source is None:
        result['reason'] = 'The conversation’s account is not known yet.'
        return result
    model = ts.get('model') or config.get('engines.claude.model')
    current = pressure(source, model)
    if hard:
        fetched = source.get('usageFetchedAt')
        sampled = dt.datetime.fromisoformat(fetched.replace('Z','+00:00')).timestamp() if fetched else 0
        if current is not None and current < threshold and sampled > ts.get('last_ts',time.time()):
            result.update(current_ready=True,reason='A newer quota reading shows that the current account has recovered.')
            return result
        current = 100.  # An explicit quota failure is stronger evidence than a cached percentage.
    if current is None or current < threshold:
        result['reason'] = 'The current account is not near a confirmed quota limit.'
        return result
    if not hard:
        with _store() as store:
            last = store.execute('SELECT max(at) FROM moves WHERE session=?', (ts.get('session_id'),)).fetchone()[0]
        if last is not None and time.time() - last < cooldown:
            result['reason'] = 'This conversation recently changed accounts.'
            return result
    budget, reason = claude_budget.estimate(con, dict(ts, _quota_plan=(source.get('plan') or {})))
    if not budget:
        result['reason'] = reason
        return result
    if budget['required_percent'] >= 100:
        result['reason'] = 'The cold-resume estimate leaves too little working room even on an empty account.'
        return result
    choices = []
    for row in rows:
        if all(row.get(k) == source.get(k) for k in ('email', 'organizationUuid')):
            continue
        pct = pressure(row, model)
        if pct is None or float(row.get('usageAgeSeconds', math.inf)) > 300:
            continue
        # Historical calibration is transferable only between the same
        # currently verified subscription tier, never Pro to a weaker plan.
        if any((row.get('plan') or {}).get(k) != (source.get('plan') or {}).get(k) for k in ('label', 'tier')):
            continue
        committed = reserved(row, session=ts.get('session_id'))
        drift = max(0, float(row['usageAgeSeconds']) / 60)
        available = 100 - pct - committed - drift
        if current - pct < margin or available < budget['required_percent']:
            continue
        choices.append((pct + committed, row, available, drift))
    if not choices:
        result['reason'] = 'No other account has verified room for the cold resume plus a continuation reserve.'
        return result
    pct, target, available, drift = min(choices, key=lambda item: (item[0], item[1]['number']))
    result['candidate'] = dict(budget, source={k:source[k] for k in ('email','organizationUuid')},
                               target={k:target[k] for k in ('email','organizationUuid')},
                               target_label=target.get('alias') or target['email'], source_pressure=current,
                               target_pressure=pressure(target, model), available_percent=available, drift_percent=drift)
    return result


def commit_switch(session, plan, token, *, hard=False):
    """Commit a freshly rechecked choice; reserve budget against racing topics."""
    now = time.time()
    previous, target = plan['source'], plan['target']
    with _store() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT identity FROM bindings WHERE session=?', (session,)).fetchone()
        if not row or json.loads(row[0]) != previous:
            raise AccountError('The conversation account changed while checking the switch.')
        if not hard:
            last = con.execute('SELECT max(at) FROM moves WHERE session=?', (session,)).fetchone()[0]
            if last is not None and now - last < float(config.get('engines.claude.accounts.cooldown_seconds', 1800)):
                raise AccountError('This conversation recently changed accounts.')
        available = 100 - plan['target_pressure'] - plan['drift_percent'] - reserved(target, session=session, con=con)
        if available < plan['required_percent']:
            raise AccountError('Another conversation reserved the available account capacity.')
        con.execute('UPDATE bindings SET identity=? WHERE session=?', (json.dumps(target), session))
        con.execute('INSERT INTO moves(session,at,previous,target,source_pressure,target_pressure) VALUES(?,?,?,?,?,?)',
                    (session,now,json.dumps(previous),json.dumps(target),plan['source_pressure'],plan['target_pressure']))
        con.execute('INSERT INTO reservations VALUES(?,?,?,?,?)',
                    (token,session,json.dumps(target),plan['required_percent'],now+900))


def select(resume: str | None, model: str | None) -> dict:
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
    if owner is not None:
        return owner
    candidates = [(pressure(row, model), row) for row in rows]
    candidates = [(pct + reserved(row, session=resume) if pct is not None else None, row) for pct,row in candidates]
    candidates = [(pct, row) for pct, row in candidates if pct is not None and pct < 100]
    if not candidates:
        raise AccountError("No Claude subscription has verified capacity for a new conversation; check herald usage claude")
    # New conversations have no established conversation cache to preserve.
    # Balance five-hour usage first, then weekly usage; both must have room.
    return target(min(candidates, key=lambda item: (
        float(((item[1].get("usage") or {}).get("fiveHour") or {}).get("pct", 0)) + reserved(item[1], session=resume),
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
