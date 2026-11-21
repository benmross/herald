"""A stopped Telegram task waits for its owner's choice, without inference.

The optional topic record is additive: old topic cursors and already queued
limit_resume records keep their meaning. Nothing is scheduled by making a card.
"""
from __future__ import annotations

import math
import re
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
ACCOUNT_CONTINUE = ("[Herald wrote this message, not the user. Continue the stopped task "
    "in the same Claude conversation on the selected account, under the configured "
    "account-continuation policy. Check what already happened before repeating any "
    "action or tool call. Earlier approvals do not carry over.]")


def hard_quota(error: str | None) -> bool:
    text = (error or '').lower()
    if any(word in text for word in ('overloaded', 'unrecognized_model', 'server-side issue', '529')):
        return False
    return bool(re.search(r"(?:you(?:'ve| have)? hit your|reached your|exceeded your).*?(?:5.hour|session|weekly|week|usage).*?limit|quota_exhausted|usage_limit_reached", text))


def account_plan(con, ts, *, hard=False):
    from . import claude_accounts
    if ts.get('engine') != 'claude' or not claude_accounts.enabled():
        return {'candidate': None, 'account_count': 0, 'reason': ''}
    try:
        return claude_accounts.switch_plan(con, ts, hard=hard)
    except (claude_accounts.AccountError, OSError, ValueError):
        return {'candidate': None, 'account_count': 0, 'reason': 'Account usage could not be verified.'}


def clip(text: str, size: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= size else text[:size - 1].rstrip() + "…"


def capacity(con, engine: str) -> dict | None:
    """Positive evidence of capacity, never an absent or failed reading."""
    if engine not in config.enabled_engines():
        return None
    try:
        from . import claude_accounts
        snapshot = (claude_accounts.snapshot(model=config.get("engines.claude.model"))
                    if engine == "claude" and claude_accounts.enabled() else usage.refresh(con, engine))
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


def make(con, ts: dict, progress: str = "", request: str = "", *, warning=False,
         hard_limit=False, inspection=None) -> tuple[dict, str, list]:
    engine = ts["engine"]
    now = time.time()
    from . import claude_accounts
    if engine == "claude" and claude_accounts.enabled():
        try:
            windows = claude_accounts.snapshot(ts.get("session_id"), ts.get("model") or config.get("engines.claude.model"))["windows"]
            resets = [w["resets_at"] for w in windows if w["resets_at"] and w["resets_at"] > now
                      and w["used_percent"] >= limitresume.FULL_PERCENT]
            reset = max(resets) if resets else None
        except (claude_accounts.AccountError, OSError, ValueError):
            reset = None
    else:
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
    inspection = inspection if inspection is not None else account_plan(con, ts, hard=hard_limit)
    candidate = inspection.get('candidate')
    record.update(mode='warning' if warning else 'limit', hard_limit=hard_limit)
    if inspection.get('account_count',0)>1:
        record['model_snapshot']={'model':ts.get('model') or config.get(f'engines.{engine}.model'),
                                  'effort':ts.get('effort') if ts.get('effort') is not None else config.get(f'engines.{engine}.effort')}
    if candidate:
        record['account'] = candidate
    if inspection.get('current_ready'):
        record['current_ready'] = True
    if warning:
        record['default_at'] = None
    elif hard_limit and inspection.get('account_count', 0) > 1 and not inspection.get('switching_disabled'):
        record.update(default_at=now+60, default_action='account_or_wait')
    lines = [f"{engine.capitalize()} is near its usage limit. This task is paused before starting." if warning else
             f"{engine.capitalize()} hit its usage limit. This task is paused."]
    source = inspection.get('source') or {}
    if source.get('email'):
        lines.append('Current Claude account: ' + (source.get('alias') or source['email']))
    if request:
        lines.append("Task: " + clip(request, 300))
    if progress:
        lines.append("Last update: " + clip(progress, 600))
    lines.append(f"The limit resets {clock(reset)}." if reset else
                 "I couldn't verify the reset time, so waiting means a retry in 15 minutes.")
    buttons = []
    if candidate:
        lines.append(f"Switch account: {candidate['target_label']} ({candidate['target_pressure']:g}% used in its most-used applicable window). The cold-resume check includes "
                     f"about {candidate['cold_tokens']:,} context tokens and a continuation reserve. "
                     "Your model, effort and conversation stay the same.")
        buttons.append([{'text': 'Switch Claude account', 'callback_data': f'limit:{token}:account'}])
    elif inspection.get('reason') and inspection.get('account_count', 0) > 1:
        lines.append('Account switch unavailable: ' + inspection['reason'])
    if warning or inspection.get('current_ready'):
        buttons.append([{'text':'Continue current account', 'callback_data':f'limit:{token}:keep'}])
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
    default_model = config.get(f"engines.{engine}.model")
    if default_model and ts.get("model") and ts["model"] != default_model:
        effort = config.get(f"engines.{engine}.effort")
        record["default_target"] = {"model": default_model, "effort": effort, "fresh": True}
        lines.append(f"After reset, you can also continue on {engine.capitalize()} / "
                     f"{default_model}, effort {effort or 'CLI default'}, in a new session "
                     "with the saved work and previous transcript carried over.")
        buttons.append([{"text": f"Continue on {default_model} after reset",
                         "callback_data": f"limit:{token}:defaultwait"}])
    if warning:
        lines.append("Choose an option to continue. This warning will not switch accounts on its own.")
    elif record.get('default_action') == 'account_or_wait':
        lines.append("If you don't choose within one minute, I'll recheck quota. I'll keep this account if it has recovered; "
                     "otherwise I'll switch only if the other account still safely fits the cold resume. "
                     "If neither is ready, I'll wait for a verified reset. "
                     "Previous approvals do not carry over.")
    else:
        lines.append(f"If you don't choose, I'll continue on {engine.capitalize()} in this session "
                     "once the limit resets. Previous approvals do not carry over.")
    buttons += [[{"text": "Wait for this account’s reset" if inspection.get('account_count',0)>1 else "Continue when limit resets", "callback_data": f"limit:{token}:wait"}],
                [{"text": "Stop", "callback_data": f"limit:{token}:stop"}]]
    return record, "\n\n".join(lines), buttons


def wait_at(con, ts, *, now=None, earliest=False):
    """Use the owning account, never the unrelated default login's reset."""
    from . import claude_accounts
    now = time.time() if now is None else now
    if ts.get('engine') == 'claude' and claude_accounts.enabled():
        try:
            from . import claude_budget
            rows = claude_accounts.accounts()
            saved = claude_accounts.binding(ts.get('session_id'))
            model = ts.get('model') or config.get('engines.claude.model')
            source = next((r for r in rows if saved and all(r.get(k)==v for k,v in saved.items())),{})
            budget, _ = claude_budget.estimate(con, ts) if earliest and claude_accounts.switching_enabled() else (None, '')
            times = []
            for row in rows:
                owner = saved and all(row.get(k)==v for k,v in saved.items())
                if not owner and any((row.get('plan') or {}).get(k)!=(source.get('plan') or {}).get(k) for k in ('label','tier')):
                    continue
                pct = claude_accounts.pressure(row, model)
                if pct is None or not (owner or earliest and budget and budget['required_percent']<100):
                    continue
                cutoff = (float(config.get('engines.claude.accounts.switch_threshold',90)) if ts.get('_account_warning') else
                          limitresume.FULL_PERCENT) if owner else 100-budget['required_percent']
                resets = []
                for _, window in claude_accounts.windows(row, model):
                    if window['pct'] >= cutoff:
                        reset = window.get('resetsAt')
                        if not reset:
                            resets = []
                            break
                        stamp = datetime.fromisoformat(reset.replace('Z','+00:00')).timestamp()
                        if stamp>now:
                            resets.append(stamp)
                if resets:
                    times.append(max(resets)+limitresume.GRACE_SECONDS)
            return min(times) if times else now+limitresume.RETRY_SECONDS
        except (claude_accounts.AccountError, OSError, ValueError):
            return now+limitresume.RETRY_SECONDS
    return limitresume.when(con, ts['engine'], now)


def current(ts: dict, record: dict) -> bool:
    return (ts.get(KEY) is record and all(ts.get(k) == record.get(k)
            for k in ("engine", "session_id", "last_ts", "model", "effort")))
