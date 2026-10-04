"""Asking for a command to run as root, and setting up what answers.

The other half of `bin/herald-rootd`; read its docstring for why the design is
shaped the way it is. In one line: a session writes a request into the spool,
the daemon shows the exact command on a Telegram bot only it can read, and
runs it as root only on the owner's tap.

What lives here runs as the user and is deliberately powerless. `request()`
can only drop a file in a directory the user already owns, and nothing it
writes is trusted by the daemon beyond its shape. The approvals and actions
rows written here are the ledger's *record* of what happened, so the digest and
`herald db` can see it like any other red action. They are not the gate. The
gate is the tap on the second bot, which is checked in a root process against a
root-only file, so editing these rows changes the record and nothing else.

`setup()`, `update()` and `remove()` run as root, under the sudo that
`herald root setup` asks for, and install the daemon as a root-owned copy.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import pathlib
import platform
import pwd
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid

from . import approvals, config, db

UNIT = "herald-root.service"
CONFIG_DIR = pathlib.Path("/etc/herald-root")
CONFIG_PATH = CONFIG_DIR / "config.json"
INSTALL_DIR = pathlib.Path("/usr/local/lib/herald-root")
INSTALLED = INSTALL_DIR / "herald-rootd"
UNIT_PATH = pathlib.Path("/etc/systemd/system") / UNIT
SOURCE = config.ROOT / "bin" / "herald-rootd"
UNIT_SOURCE = config.ROOT / "systemd" / "system" / UNIT

#: the daemon's own limits, mirrored so a bad request fails here, not there
TAP_WINDOW = 15 * 60
MAX_COMMAND = 3000
MAX_TIMEOUT = 3600

#: a result's states, as the daemon writes them
RAN = ("done", "failed", "timeout")


def spool() -> pathlib.Path:
    return config.HOME / "run" / "root"


# --------------------------------------------------------------------------
# State, readable by anyone
# --------------------------------------------------------------------------

def supported() -> bool:
    return platform.system() == "Linux" and shutil.which("systemctl") is not None


def active() -> bool:
    if not supported():
        return False
    r = subprocess.run(["systemctl", "is-active", "--quiet", UNIT])
    return r.returncode == 0


def installed() -> bool:
    return UNIT_PATH.exists()


def _sha(path: pathlib.Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def current() -> bool:
    """Is the installed daemon the one in this checkout?

    It is a copy on purpose (see herald-rootd), so an update to the program
    does not reach it until the user re-runs setup under sudo. This is how
    `herald doctor` notices that it has fallen behind.
    """
    return _sha(INSTALLED) is not None and _sha(INSTALLED) == _sha(SOURCE)


def status() -> dict:
    return {"supported": supported(), "installed": installed(), "active": active(),
            "current": current() if installed() else None,
            "spool": str(spool())}


# --------------------------------------------------------------------------
# Asking, as the user
# --------------------------------------------------------------------------

class Unavailable(RuntimeError):
    pass


def request(command: str, *, why: str = "", cwd: str = "/", timeout: int = 600,
            actor: str = "session") -> tuple[str, int]:
    """Spool one request. Returns (request id, approvals row id)."""
    if not active():
        raise Unavailable(
            "root approvals are not set up on this machine. Give the user the "
            "command to run themselves, or ask them to run `herald root setup`.")
    if not command.strip():
        raise ValueError("empty command")
    if len(command) > MAX_COMMAND:
        raise ValueError(f"command longer than {MAX_COMMAND} characters")
    if not 1 <= timeout <= MAX_TIMEOUT:
        raise ValueError(f"timeout must be 1-{MAX_TIMEOUT} seconds")
    rid = uuid.uuid4().hex
    payload = {"command": command, "why": why, "cwd": cwd, "timeout": timeout,
               "actor": actor, "requested_at": time.time()}
    for sub in ("requests", "results"):
        (spool() / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
    approval_id = approvals.request(actor=actor, kind="root.exec", target=socket.gethostname(),
                                    summary=f"run as root: {command[:120]}",
                                    payload={**payload, "request_id": rid})
    tmp = spool() / "requests" / f".{rid}.tmp"
    tmp.write_text(json.dumps(payload))
    tmp.rename(spool() / "requests" / f"{rid}.json")
    return rid, approval_id


def wait(rid: str, *, timeout: int) -> dict:
    """Block until the daemon writes a result, or give up."""
    path = spool() / "results" / f"{rid}.json"
    deadline = time.monotonic() + TAP_WINDOW + timeout + 60
    while time.monotonic() < deadline:
        if path.exists():
            try:
                result = json.loads(path.read_text())
            except (OSError, ValueError):
                time.sleep(0.5)
                continue
            path.unlink(missing_ok=True)
            return result
        time.sleep(1)
    return {"id": rid, "state": "lost",
            "note": "no result from herald-rootd; check `journalctl -u herald-root`"}


def record(approval_id: int, result: dict, *, actor: str) -> None:
    """Mirror the daemon's outcome into the ledger's audit tables."""
    state = result.get("state")
    if state in RAN:
        approvals.decide(approval_id, approvals.APPROVED, by="herald-rootd")
        with db.session() as con:
            db.record_action(
                con, actor=actor, tier="red", kind="root.exec",
                target=socket.gethostname(),
                summary=f"ran as root ({state}, exit {result.get('exit_code')})",
                ref=f"herald-root:{result.get('id')}", approval_id=approval_id)
            con.commit()
        approvals.mark_done(approval_id)
    elif state == "denied":
        approvals.decide(approval_id, approvals.DENIED, by="herald-rootd")
    else:
        with db.session() as con:
            con.execute("UPDATE approvals SET state = ? WHERE id = ? AND state = ?",
                        (approvals.EXPIRED, approval_id, approvals.PENDING))
            con.commit()


def run(command: str, *, why: str = "", cwd: str = "/", timeout: int = 600,
        actor: str = "session") -> dict:
    rid, approval_id = request(command, why=why, cwd=cwd, timeout=timeout, actor=actor)
    result = wait(rid, timeout=timeout)
    record(approval_id, result, actor=actor)
    return result


# --------------------------------------------------------------------------
# Setup, as root
# --------------------------------------------------------------------------

def _api(token: str, method: str, **params) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(params).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        try:
            return json.load(exc)
        except Exception:                                           # noqa: BLE001
            return {"ok": False, "description": f"HTTP {exc.code}"}
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "description": str(exc)}


def _install_files() -> None:
    INSTALL_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    os.chown(INSTALL_DIR, 0, 0)
    tmp = INSTALL_DIR / ".herald-rootd.tmp"
    shutil.copyfile(SOURCE, tmp)
    os.chown(tmp, 0, 0)
    os.chmod(tmp, 0o755)
    os.replace(tmp, INSTALLED)
    shutil.copyfile(UNIT_SOURCE, UNIT_PATH)
    os.chmod(UNIT_PATH, 0o644)
    subprocess.run(["systemctl", "daemon-reload"], check=True)


def _owner_from_main_bot() -> int | None:
    try:
        return int(config.secret("telegram.user_id"))
    except (TypeError, ValueError):
        return None


def setup(user: str) -> int:
    """Interactive. Creates the root-only config, installs and starts the unit."""
    if os.geteuid() != 0:
        print("this step runs under sudo; `herald root setup` asks for it")
        return 1
    pw = pwd.getpwnam(user)
    if pw.pw_uid == 0:
        print("run `herald root setup` as the user Herald runs as, not as root")
        return 1
    print("\nHerald root approvals\n")
    print("Herald's sessions cannot use sudo. This lets one ask: you get the exact")
    print("command on Telegram, and it runs as root only if you tap yes.\n")
    print("The question comes from a second bot, not Herald's usual one. The usual")
    print("bot's token is readable by every session, so a tap that arrives through")
    print("it is not proof you tapped. This one's token is stored where only root")
    print("can read it.\n")
    print("1. In Telegram, message @BotFather, send /newbot, and pick any name.")
    print("2. Paste the token it gives you below. It is not shown as you type.\n")
    token = getpass.getpass("Token: ").strip()
    me = _api(token, "getMe")
    if not me.get("ok"):
        print(f"That token did not work: {me.get('description')}")
        return 1
    if token == config.secret("telegram.bot_token"):
        print("That is Herald's main bot. It has to be a new one, for the reason above.")
        return 1
    name = me["result"]["username"]
    print(f"\n3. Open https://t.me/{name} on your phone and press Start.")
    print("   Waiting up to five minutes...")
    _api(token, "deleteWebhook")
    offset, owner, deadline = 0, None, time.monotonic() + 300
    while owner is None and time.monotonic() < deadline:
        resp = _api(token, "getUpdates", timeout=25, offset=offset)
        for upd in resp.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            if (msg.get("chat") or {}).get("type") == "private" and msg.get("from"):
                owner = msg["from"]["id"]
    if owner is None:
        print("Nothing arrived. Run `herald root setup` again when you are ready.")
        return 1
    _api(token, "getUpdates", offset=offset)          # consume what we read
    expected = _owner_from_main_bot()
    if expected is not None and owner != expected:
        print(f"\nThat Start came from Telegram user {owner}, but Herald's main bot")
        print(f"belongs to user {expected}. Only one person should be able to approve.")
        if input("Use this account anyway? [y/N] ").strip().lower() != "y":
            return 1
    groups = sorted({g for g in os.getgrouplist(user, pw.pw_gid)})
    cfg = {"token": token, "owner_id": owner, "user": user, "uid": pw.pw_uid,
           "gid": pw.pw_gid, "groups": groups, "spool": str(spool()),
           "host": socket.gethostname(), "bot": name}
    CONFIG_DIR.mkdir(mode=0o700, exist_ok=True)
    os.chown(CONFIG_DIR, 0, 0)
    os.chmod(CONFIG_DIR, 0o700)
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG_PATH, 0o600)
    for sub in ("", "requests", "results"):
        d = spool() / sub
        d.mkdir(parents=True, exist_ok=True)
        os.chown(d, pw.pw_uid, pw.pw_gid)
        os.chmod(d, 0o700)
    for parent in (spool().parent,):
        os.chown(parent, pw.pw_uid, pw.pw_gid)
    _install_files()
    subprocess.run(["systemctl", "enable", "--now", UNIT], check=True)
    subprocess.run(["systemctl", "restart", UNIT], check=True)
    _api(token, "sendMessage", chat_id=owner,
         text=f"Set up. Herald will ask here before it runs anything as root on "
              f"{socket.gethostname()}, and nothing runs without your tap.")
    print(f"\nDone. herald-root is running, and @{name} will ask before anything runs.")
    print("Try it: herald root run --why 'test' -- id")
    return 0


def update() -> int:
    """Re-install the daemon from this checkout, keeping the config."""
    if os.geteuid() != 0:
        print("this step runs under sudo; `herald root update` asks for it")
        return 1
    if not CONFIG_PATH.exists():
        print("not set up yet: run `herald root setup`")
        return 1
    _install_files()
    subprocess.run(["systemctl", "restart", UNIT], check=True)
    print("herald-root updated and restarted.")
    return 0


def remove() -> int:
    if os.geteuid() != 0:
        print("this step runs under sudo; `herald root remove` asks for it")
        return 1
    subprocess.run(["systemctl", "disable", "--now", UNIT])
    for p in (UNIT_PATH, INSTALLED, CONFIG_PATH):
        p.unlink(missing_ok=True)
    for d in (INSTALL_DIR, CONFIG_DIR):
        try:
            d.rmdir()
        except OSError:
            pass
    subprocess.run(["systemctl", "daemon-reload"])
    print("herald-root removed. The second bot still exists; delete it with "
          "@BotFather (/deletebot) if you like.")
    return 0
