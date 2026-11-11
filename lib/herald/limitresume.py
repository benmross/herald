"""Continue a Telegram turn that a usage limit stopped, once the limit has reset.

A turn that runs out of quota part-way ends as a failure, and the work waits
for the user to notice and say "continue". Someone who starts a long job and
goes to sleep expecting the limit to be hit loses every hour between the reset
and waking up. With `engines.resume_after_limit` on, the bridge instead notes
when the limit lifts, says so, and at that time gives the topic one message,
written by Herald and marked as such, asking the session to carry on.

It stays opt-in because the default rule is that a failure stays a failure and
nothing runs that the user did not send. It buys nothing and switches no
provider: under Auto a topic that could move to another provider has already
moved, and this only covers a turn that still ended at a limit. A message from
the user, or /stop, cancels a pending continuation; approvals never carry over
into the continued turn.

The time comes from a quota reading, never from the error text. A limit with
no reading to explain it (an overloaded API, a reading that could not be
taken) is retried after `RETRY_SECONDS` instead, and a topic gives up after
`MAX_ATTEMPTS` continuations in a row that each ended at a limit.
"""
from __future__ import annotations

import json
import uuid

from . import config, usage

KEY = "limit_resume"
ATTEMPTS = "limit_resume_attempts"
MAX_ATTEMPTS = 6
RETRY_SECONDS = 15 * 60
#: After the stated reset, since the provider's clock and this one differ.
GRACE_SECONDS = 90
#: A window this full is taken to be the one that stopped the turn.
FULL_PERCENT = 95.0

PROMPT = ("[Herald wrote this message, not the user. The usage limit that stopped your "
          "last turn in this topic has reset. Continue the work you were doing from where "
          "it stopped. First check what is already done (files, commits, the ledger) so "
          "nothing is repeated. The user has not said anything new: approvals from before "
          "do not carry over, and anything that needs one still waits for them.]")


def enabled() -> bool:
    return bool(config.get("engines.resume_after_limit", False))


def _windows(con, engine: str) -> list[dict]:
    try:
        return usage.refresh(con, engine)["windows"]
    except Exception:                                         # noqa: BLE001
        # A reading that cannot be taken now must not lose the continuation:
        # fall back to the last saved one, and to a plain retry after that.
        row = con.execute("SELECT data FROM facts WHERE source=? AND kind='snapshot' "
                          "AND external_id=?", (usage.SOURCE, engine)).fetchone()
        try:
            return json.loads(row[0])["windows"] if row else []
        except (ValueError, KeyError, TypeError):
            return []


def when(con, engine: str, now: float) -> float:
    """The time to continue: after the last full quota window resets."""
    resets = [w["resets_at"] for w in _windows(con, engine)
              if w.get("resets_at") and w["resets_at"] > now
              and w.get("used_percent", 0) >= FULL_PERCENT]
    return max(resets) + GRACE_SECONDS if resets else now + RETRY_SECONDS


def schedule(ts: dict, at: float, engine: str, chat_id: int, thread_id: int | None) -> bool:
    """Note a continuation on the topic. False once it has tried too often."""
    attempts = int(ts.get(ATTEMPTS, 0)) + 1
    if attempts > MAX_ATTEMPTS:
        ts.pop(KEY, None)
        ts.pop(ATTEMPTS, None)
        return False
    ts[ATTEMPTS] = attempts
    ts[KEY] = {"at": at, "engine": engine, "chat_id": chat_id, "thread_id": thread_id}
    return True


def cancel(ts: dict) -> bool:
    """Drop a pending continuation. True if there was one."""
    ts.pop(ATTEMPTS, None)
    return ts.pop(KEY, None) is not None


def request(topic: str, ts: dict, at: float) -> str:
    """Queue an existing stopped session without editing the bridge's live state.

    This also covers limits hit before auto-resume was loaded. The bridge
    imports the request into its normal, cancellable continuation state.
    A newer turn or a changed session makes the request obsolete.
    """
    if not ts.get("session_id") or not ts.get("engine"):
        raise ValueError("a continuation needs an existing session and engine")
    folder = config.LOGS / "limit-resume-requests"
    folder.mkdir(parents=True, exist_ok=True)
    request_id = uuid.uuid4().hex
    path = folder / (request_id + ".json")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"topic": topic, "session_id": ts["session_id"],
                                    "last_ts": ts.get("last_ts", 0),
                                    "engine": ts["engine"], "at": at}))
    temporary.replace(path)
    return request_id


def import_requests(threads: dict) -> bool:
    """Import one-shot requests, preserving any newer user activity."""
    imported = False
    for path in sorted((config.LOGS / "limit-resume-requests").glob("*.json")):
        record = json.loads(path.read_text())
        key = record["topic"]
        ts = threads.get(key, {})
        if (ts.get("session_id") == record["session_id"]
                and ts.get("engine") == record["engine"]
                and ts.get("last_ts", 0) == record["last_ts"]
                and KEY not in ts):
            chat, thread = key.split(":", 1)
            imported |= schedule(ts, record["at"], record["engine"], int(chat),
                                 None if thread == "main" else int(thread))
        path.unlink()
    return imported


def take_due(threads: dict, now: float) -> list[tuple[str, dict]]:
    """Pending continuations whose time has come, removed so each fires once."""
    due = []
    for key, ts in threads.items():
        record = ts.get(KEY)
        if isinstance(record, dict) and record.get("at", 0) <= now:
            due.append((key, ts.pop(KEY)))
    return due
