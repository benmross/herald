"""Taking a secret from the user's phone without it passing through a model.

A session that needs an API key has had two options, both bad. It could ask the
user to paste the key into chat, where it lands on the chat provider's servers,
in the model's context and in the session transcript on disk. Or it could ask
them to open a shell on the server and edit `secrets.json` by hand, which from
a phone means they do it later or not at all.

This is the third option. A session runs `herald secret request <path>`, which
writes a request into a spool and sends the user a one-time link. The link
opens a page served by `bin/herald-secrets`, a small stdlib HTTP server bound
to localhost and exposed to the user's own devices by whatever the install
puts in front of it (on a tailnet, `tailscale serve`). They paste the value,
the server writes it with `config.set_secret`, and the session is told only
that the path is now set.

What holds it together:

- **The value never comes back out.** No endpoint returns it, the spool never
  holds it, the server never logs it, and the confirmation names the path only.
- **A link works once and expires.** The token is 192 random bits. The spool
  holds its SHA-256, so reading the spool does not yield a usable link.
- **Every stored value is logged** as an `actions` row (green: the user asked,
  and it touches only their own machine), so the digest shows it.
- **The page says whose words the reason is.** The `--why` is written by a
  session and shown labelled as such, so a request that something fetched has
  talked a session into making reads as what it is.

A session can still write `secrets.json` through Python, as it always could.
This module adds a way in for the user and no new way out for anyone.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re
import secrets as _random
import time

from . import config, db

PATH_RE = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+){0,5}$")
MAX_VALUE = 64 * 1024
MAX_WHY = 300
DEFAULT_MINUTES = 15
MAX_MINUTES = 24 * 60


def spool() -> pathlib.Path:
    return config.HOME / "run" / "secrets"


def port() -> int:
    return int(config.get("surfaces.secret_intake.port", 8792))


def base_url() -> str | None:
    url = config.get("surfaces.secret_intake.url")
    return str(url).rstrip("/") if url else None


def enabled() -> bool:
    return bool(base_url())


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> float:
    return time.time()


def _stamp(t: float) -> str:
    return dt.datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# What is already there, without saying what it is

def _lookup(path: str):
    """(exists, node, blocker): whether `path` holds a value, and if not,
    the dotted prefix that would be clobbered by writing it, if any."""
    node = config.secrets()
    parts = path.split(".")
    for i, part in enumerate(parts):
        if not isinstance(node, dict):
            return False, None, ".".join(parts[:i])
        if part not in node:
            return False, None, None
        node = node[part]
    return True, node, None


def has(path: str) -> bool:
    config._secrets = None  # another process may have written it since
    exists, node, _ = _lookup(path)
    return exists and node not in (None, "", {})


def check_path(path: str) -> str | None:
    """Why `path` cannot be requested, or None if it can."""
    if not PATH_RE.match(path or ""):
        return ("a secret path is one to six dotted names of letters, digits, "
                "'_' and '-', such as openai.api_key")
    config._secrets = None
    exists, node, blocker = _lookup(path)
    if blocker:
        return (f"{blocker} already holds a value that is not a group, and "
                f"writing {path} would replace it")
    if exists and isinstance(node, dict):
        return (f"{path} is a group of {len(node)} values; writing one value "
                "there would delete them. Name the key inside it.")
    return None


# --------------------------------------------------------------------------
# Requests

def request(path: str, *, why: str, as_json: bool = False,
            minutes: int = DEFAULT_MINUTES, actor: str = "session",
            thread_id: int | None = None) -> tuple[dict, str]:
    """Open a request and return (the request, the link). Sends nothing."""
    if problem := check_path(path):
        raise ValueError(problem)
    why = " ".join((why or "").split())
    if not why:
        raise ValueError("say why with --why: the user sees it on the page")
    if len(why) > MAX_WHY:
        raise ValueError(f"--why is limited to {MAX_WHY} characters")
    if not 1 <= minutes <= MAX_MINUTES:
        raise ValueError(f"--minutes must be between 1 and {MAX_MINUTES}")
    base = base_url()
    if not base:
        raise LookupError(
            "no intake URL is set. Expose 127.0.0.1:%d to your phone (on a "
            "tailnet: `tailscale serve --bg --https=<port> http://127.0.0.1:%d`), "
            "then `herald config set surfaces.secret_intake.url https://...`"
            % (port(), port()))
    token = _random.token_hex(24)
    now = _now()
    req = {
        "id": _hash(token)[:10],
        "path": path,
        "why": why,
        "json": bool(as_json),
        "replaces": has(path),
        "actor": actor,
        "thread_id": thread_id,
        "created": now,
        "expires": now + minutes * 60,
        "state": "pending",
    }
    d = spool()
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    _write(_hash(token), req)
    return req, f"{base}/s/{token}"


def _write(key: str, req: dict) -> None:
    target = spool() / f"{key}.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(req, indent=1))
    tmp.replace(target)


def _state(req: dict) -> str:
    if req["state"] == "pending" and _now() > req["expires"]:
        return "expired"
    return req["state"]


def find(token: str) -> dict | None:
    """The live request a token opens, or None. Never says which reason."""
    if not re.fullmatch(r"[0-9a-f]{48}", token or ""):
        return None
    f = spool() / f"{_hash(token)}.json"
    if not f.is_file():
        return None
    req = json.loads(f.read_text())
    return req if _state(req) == "pending" else None


def all_requests() -> list[dict]:
    out = []
    if not spool().is_dir():
        return out
    for f in spool().glob("*.json"):
        try:
            req = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        req["state"] = _state(req)
        out.append(req)
    return sorted(out, key=lambda r: r["created"], reverse=True)


def get(request_id: str) -> dict | None:
    return next((r for r in all_requests() if r["id"] == request_id), None)


def wait(request_id: str, *, poll: float = 2.0) -> str:
    """Block until the request is stored or expired, and say which."""
    while True:
        req = get(request_id)
        if req is None:
            return "missing"
        if req["state"] != "pending":
            return req["state"]
        time.sleep(poll)


def store(token: str, raw: str) -> dict:
    """Write the submitted value. Raises ValueError with a message the user
    can read on the page; the value itself never appears in one."""
    req = find(token)
    if req is None:
        raise LookupError("expired or used")
    value = (raw or "").strip()
    if not value:
        raise ValueError("Nothing was pasted.")
    if len(value.encode()) > MAX_VALUE:
        raise ValueError(f"That is longer than {MAX_VALUE // 1024} KB.")
    if req["json"]:
        try:
            value = json.loads(value)
        except ValueError:
            raise ValueError("This one has to be JSON, and what was pasted "
                             "does not parse.") from None
    # Checked again at write time: the file may have changed since the request.
    if problem := check_path(req["path"]):
        raise ValueError(problem)
    config.set_secret(req["path"], value)
    req["state"] = "stored"
    req["stored"] = _now()
    _write(_hash(token), req)
    summary = (f"stored a value at secrets.{req['path']} from the intake page"
               + (", replacing the old one" if req["replaces"] else ""))
    with db.session() as con:
        db.record_action(con, actor="secret-intake", tier="green",
                         kind="secret.store", target=f"secrets.{req['path']}",
                         summary=summary, ref=req["id"])
        con.commit()
    return req


def prune(days: int = 7) -> int:
    """Drop finished requests older than `days`. They hold no value, only
    the record of what was asked, and the actions table keeps that."""
    cutoff = _now() - days * 86400
    n = 0
    for f in spool().glob("*.json") if spool().is_dir() else ():
        try:
            req = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if _state(req) != "pending" and req["created"] < cutoff:
            f.unlink(missing_ok=True)
            n += 1
    return n
