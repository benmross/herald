"""Who is actively mid-turn, right now, across Herald's conversational surfaces.

Restarting a service out from under a live turn kills it before its reply goes
out. Telegram's send() only happens after the turn's subprocess exits, so a
`systemctl restart` issued while that turn is still running kills the very
process that was going to deliver the answer -- silently, with nothing left to
explain why the user never heard back.

The design this exists for: a session that just edited Herald's own code never
restarts anything itself. It calls `herald restart --defer`, which only ever
writes a request file (see `request_restart` / `pending_restart`) -- always
safe, even from inside the live turn asking for it. `bin/herald-telegram`'s
own dispatcher is what actually watches for that request and acts on it, and
it only ever checks *after* a turn's Turn has already exited, never from
inside one -- so there is no "am I my own blocker" question to answer here.

One JSON file per active turn, named by key, under `logs/active/`. Writing and
deleting a uniquely-named file has no interleaving hazard even across several
turns running as separate threads -- there is no shared file two turns could
corrupt by writing at once, and no lock to forget to release.
"""

from __future__ import annotations

import json
import os
import time

from . import config

ACTIVE_DIR = config.LOGS / "active"
RESTART_REQUEST = config.LOGS / "restart_request.json"


class Turn:
    """Context manager: mark `key` (on `service`) active for one turn."""

    def __init__(self, service: str, key: str):
        self.service = service
        self.key = key
        safe = "".join(c if c.isalnum() or c in "-_:" else "_"
                       for c in f"{service}:{key}")
        self.path = ACTIVE_DIR / f"{safe}.json"

    def __enter__(self) -> "Turn":
        ACTIVE_DIR.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "service": self.service, "key": self.key,
            "pid": os.getpid(), "started": time.time(),
        }))
        return self

    def __exit__(self, *exc) -> bool:
        self.path.unlink(missing_ok=True)
        return False


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else -- still alive
    return True


def active() -> list[dict]:
    """Every turn currently in flight, minus stale entries -- a Turn whose
    registering process died without reaching `__exit__` (a crash, a `kill
    -9`) leaves its file behind, so a dead pid is treated as not active and
    cleaned up here rather than blocking a restart forever.
    """
    if not ACTIVE_DIR.exists():
        return []
    out = []
    for path in sorted(ACTIVE_DIR.glob("*.json")):
        try:
            info = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not _pid_alive(info.get("pid")):
            path.unlink(missing_ok=True)
            continue
        out.append(info)
    return out


def request_restart(units: list[str], reason: str) -> None:
    """Ask for a restart without performing one.

    Safe to call from inside a live turn -- it only ever writes a file.
    Whichever surface next finds itself idle (see `bin/herald-telegram`'s
    main loop) is responsible for noticing this and acting on it.
    """
    RESTART_REQUEST.parent.mkdir(parents=True, exist_ok=True)
    RESTART_REQUEST.write_text(json.dumps({
        "units": units, "reason": reason, "requested_at": time.time(),
    }))


def pending_restart() -> dict | None:
    if not RESTART_REQUEST.exists():
        return None
    try:
        return json.loads(RESTART_REQUEST.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def clear_restart_request() -> None:
    RESTART_REQUEST.unlink(missing_ok=True)
