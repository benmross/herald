"""Conservative cold-resume allowance estimates from native quota history.

The token weights allocate observed work; they are not published subscription
prices. Unknown or poorly sampled histories cannot authorize a move.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
import time

from . import config

_cache = {}


def family(model):
    text = (model or "").lower()
    return next((name for name in ("opus", "sonnet", "haiku", "fable") if name in text), None)


def units(row):
    return ((row.get("input_tokens") or 0) + 2 * (row.get("cache_write") or 0)
            + .1 * (row.get("cache_read") or 0) + 5 * (row.get("output_tokens") or 0))


def calibration(con, model, plan=None):
    """Upper observed native-percent/work ratio for model-dominated windows."""
    name = family(model)
    if not name:
        return None
    now = time.time()
    key = str(config.DB_PATH), name, (plan or {}).get('label'), (plan or {}).get('tier')
    cached = _cache.get(key)
    if cached and now - cached[0] < 300:
        return cached[1]
    since = now - 14 * 86400
    from . import claude_accounts
    owners,moves,account_samples = claude_accounts.evidence(since)
    changes = {}
    for session,at,previous,target in moves:
        changes.setdefault(session,[]).append((at,previous,target))
    def owner_at(session, stamp):
        value=owners.get(session)
        history=changes.get(session,[])
        if history:
            value=history[0][1]
            for at,previous,target in history:
                if at>stamp:
                    break
                value=target
        return value
    snapshots = {}
    for row in con.execute("SELECT before_json,after_json FROM ledger_changes WHERE entity='facts' AND record_key=? AND changed_at>=? ORDER BY seq",
                           ('["subscription_usage","snapshot","claude",null]', dt.datetime.fromtimestamp(since, dt.timezone.utc).isoformat())):
        for value in row:
            if not value:
                continue
            try:
                outer = json.loads(value)
                data = json.loads(outer['data']) if isinstance(outer['data'], str) else outer['data']
                if data['sampled_at'] >= since:
                    snapshots[data['sampled_at']] = data
            except (ValueError, TypeError, KeyError):
                continue
    windows = {}
    for sampled, snapshot in snapshots.items():
        for window in snapshot.get('windows', []):
            if window.get('key') != 'session' or not window.get('resets_at'):
                continue
            reset = round(window['resets_at'] / 1800) * 1800
            windows.setdefault((None,reset), []).append((sampled, window['used_percent']))
    for identity,sampled,account in account_samples:
        if plan and any((account.get('plan') or {}).get(k)!=plan.get(k) for k in ('label','tier')):
            continue
        window=(account.get('usage') or {}).get('fiveHour')
        if not isinstance(window,dict) or not window.get('resetsAt'):
            continue
        reset=round(dt.datetime.fromisoformat(window['resetsAt'].replace('Z','+00:00')).timestamp()/1800)*1800
        windows.setdefault((identity,reset),[]).append((sampled,window['pct']))
    rows = [dict(r) for r in con.execute("SELECT ts,session_id,model,duration_ms,input_tokens,cache_read,cache_write,output_tokens FROM runs WHERE engine='claude' AND ts>=?",
            (dt.datetime.fromtimestamp(since, config.tz()).isoformat(),))]
    work = []
    for row in rows:
        try:
            end = dt.datetime.fromisoformat(row['ts']).timestamp()
            duration = max(1, (row['duration_ms'] or 0) / 1000)
            work.append((end - duration, end, units(row), family(row['model']), owner_at(row['session_id'],end-duration)))
        except (ValueError, TypeError):
            continue
    ratios = []
    first_samples = {None:min(snapshots, default=now)}
    for identity,sampled,_ in account_samples:
        first_samples[identity]=min(first_samples.get(identity,now),sampled)
    for (identity,reset), samples in windows.items():
        begin = reset - 18000
        stop = max(t for t, _ in samples)
        peak = max(p for _, p in samples)
        # Partial, saturated and sparsely sampled tails underestimate demand.
        if begin < first_samples[identity] or reset > now or stop < reset - 1800 or not 5 <= peak < 95:
            continue
        total = chosen = 0
        for start, end, amount, model_family, account_identity in work:
            # Anonymous legacy snapshots may only use unbound legacy runs.
            # Otherwise two accounts' traffic would make one look cheaper.
            if account_identity != identity:
                continue
            overlap = max(0, min(end, stop) - max(start, begin))
            share = amount * overlap / max(1, end - start)
            total += share
            if model_family == name:
                chosen += share
        if chosen >= 100_000 and total and chosen / total >= .8:
            # Attribute all measured usage to this model, including other
            # surfaces, and add two points for rounded native percentages.
            ratios.append((peak + 2) / chosen)
    result = None
    if len(ratios) >= 5:
        ratios.sort()
        result = {'percent_per_unit': ratios[math.ceil(.95 * len(ratios)) - 1],
                  'windows': len(ratios), 'model': name}
    _cache[key] = now, result
    return result


def estimate(con, ts):
    model = ts.get('model') or config.get('engines.claude.model')
    context = ts.get('context_tokens')
    if isinstance(context, bool) or not isinstance(context, (int, float)) or not math.isfinite(context) or context <= 0:
        return None, 'The conversation size is unknown, so a cold resume cannot be checked.'
    try:
        sample = calibration(con, model, ts.get('_quota_plan'))
    except (sqlite3.Error, OSError, ValueError, TypeError, AttributeError):
        sample = None
    if not sample:
        return None, 'There is not enough native usage history to check this model’s cold-resume cost.'
    # The latest prompt omits its output and possibly a final tool result.
    cold_tokens = math.ceil(context * 1.25 + 32768)
    cold_units = 2 * cold_tokens + 5 * 8192
    predicted = 2 * sample['percent_per_unit'] * cold_units
    reserve = float(config.get('engines.claude.accounts.resume_reserve_percent', 20))
    if not math.isfinite(reserve) or not 10 <= reserve < 100:
        return None, 'The configured continuation reserve is invalid.'
    return {'cold_tokens': cold_tokens, 'estimated_percent': predicted,
            'required_percent': max(30, predicted + reserve), 'calibration_windows': sample['windows']}, ''
