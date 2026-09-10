"""Locating Herald and reading its config.

Two directories, and the distinction between them is the whole reason this
module exists.

**ROOT** is the program: this checked-out repository. Code, docs, the built-in
collectors and cycles, the venv, the logs its own processes write. It is
resolved from this file's location rather than the working directory, so a
collector invoked by systemd from anywhere still finds everything.

**HOME** is the person: `$HERALD_HOME`, `~/.herald` by default. Their config,
their secrets, their ledger, their extensions. Nothing in it is Herald's to
ship, and nothing in ROOT is theirs to own.

Herald was one directory until 9 September 2026, when it became something other
people could install. Everything personal lived in the repo, so a fresh clone
was somebody else's life and there was no way to hold your own ledger anywhere
but inside the program's git history. `ROOT/ledger` is now a symlink to
`HOME/ledger` in a normal install, which is what lets every prompt, skill and
docstring keep saying `ledger/identity/` and be right.

The legacy layout is still resolved, because the instance this was extracted
from was running while it was extracted: if `$HERALD_HOME` is unset, there is
no `~/.herald/config.json`, and `ROOT/ledger` is a real directory rather than a
symlink, then HOME is ROOT and the old paths apply unchanged.
"""

from __future__ import annotations

import copy
import json
import os
import pathlib
import sys
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[2]

DEFAULTS_PATH = ROOT / "config" / "defaults.json"


def _resolve_home() -> pathlib.Path:
    env = os.environ.get("HERALD_HOME")
    if env:
        return pathlib.Path(env).expanduser().resolve()
    default = pathlib.Path.home() / ".herald"
    if (default / "config.json").exists():
        return default
    ledger = ROOT / "ledger"
    if ledger.is_dir() and not ledger.is_symlink():
        return ROOT                          # the pre-split layout
    return default


HOME = _resolve_home()
LEGACY_LAYOUT = HOME == ROOT

CONFIG_PATH = (ROOT / "config" / "herald.json") if LEGACY_LAYOUT else HOME / "config.json"
SECRETS_PATH = (ROOT / "config" / "secrets.json") if LEGACY_LAYOUT else HOME / "secrets.json"

# Identical expressions in both layouts, because ROOT/ledger is a symlink to
# HOME/ledger once the split has happened. That is the point of the symlink.
LEDGER = HOME / "ledger"
IDENTITY = LEDGER / "identity"
STATE = LEDGER / "state"
JOURNAL = LEDGER / "journal"
RAW = LEDGER / "raw"
DB_PATH = LEDGER / "facts.db"

EXTENSIONS = HOME / "extensions"

# Logs stay with the program: they are about Herald's own processes, not about
# the person, and systemd units, the watchdog and bin/herald-brain all name
# this path. Gitignored.
LOGS = ROOT / "logs"

_cache: dict | None = None
_secrets: dict | None = None


def _merge(base: dict, over: dict) -> dict:
    """Deep-merge `over` onto `base`. Lists replace rather than concatenate --
    a user who names three job feeds means those three, not those three plus
    whatever Herald shipped."""
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load() -> dict:
    """The parsed config: `config/defaults.json` with the user's config over it.

    Cached; call reload() after editing it in-process. Defaults are tracked and
    shared by every install; the user's file holds only what is theirs, so a
    default that changes in a Herald update actually reaches them instead of
    being frozen into a copy the wizard wrote once.
    """
    global _cache
    if _cache is None:
        base = json.loads(DEFAULTS_PATH.read_text()) if DEFAULTS_PATH.exists() else {}
        user = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
        _cache = _merge(base, user)
    return _cache


def reload() -> dict:
    global _cache
    _cache = None
    return load()


def secrets() -> dict:
    """Values too sensitive for the tracked config.

    `$HERALD_HOME/secrets.json`, mode 600, gitignored even inside the private
    ledger repo. Personal feed URLs, API tokens and database passwords go here
    — anything whose leak would matter even in a private repo, because a
    private repo is one visibility flip from a public one.
    """
    global _secrets
    if _secrets is None:
        _secrets = json.loads(SECRETS_PATH.read_text()) if SECRETS_PATH.exists() else {}
    return _secrets


def secret(path: str, default=None):
    """Read a dotted path out of secrets.json."""
    node = secrets()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_user(path: str, value) -> dict:
    """Write one dotted path into the user's own config and reload.

    Only ever touches `$HERALD_HOME/config.json`: `config/defaults.json` is
    the program's file and belongs to whoever packaged Herald, not to the
    person running it. Anything written here therefore survives an update,
    and anything not written here follows one.
    """
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    current = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    node = current
    parts = path.split(".")
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value
    CONFIG_PATH.write_text(json.dumps(current, indent=2) + "\n")
    return reload()


def set_secret(path: str, value) -> dict:
    """Write one dotted path into secrets.json, and keep it mode 600.

    Separate from `set_user` because the two files have different rules: config
    is versioned in the user's private repo, secrets never are. Anything that
    would matter if a repository's visibility were flipped belongs here.
    """
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    current = json.loads(SECRETS_PATH.read_text()) if SECRETS_PATH.exists() else {}
    node = current
    parts = path.split(".")
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value
    SECRETS_PATH.write_text(json.dumps(current, indent=2))
    os.chmod(SECRETS_PATH, 0o600)
    global _secrets
    _secrets = None
    return secrets()


def python() -> str:
    """The interpreter that has Herald's dependencies."""
    venv = ROOT / "venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def get(path: str, default=None):
    """Read a dotted path out of the config, e.g. get("notify.ntfy_topic")."""
    node = load()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


#: subject / object / possessive-determiner / possessive-pronoun / reflexive
_PRONOUNS = {
    "he": ("he", "him", "his", "his", "himself"),
    "she": ("she", "her", "her", "hers", "herself"),
    "they": ("they", "them", "their", "theirs", "themselves"),
    "it": ("it", "it", "its", "its", "itself"),
}


def person() -> dict:
    """Who this Herald belongs to, in the forms text about them needs.

    Snapshots and journals are written *about* the user and read by the agent,
    so they need a name and pronouns; anything the user themselves reads is
    second person and needs neither. Unset pronouns default to they/them --
    a wrong guess misgenders a real person in a way the neutral form never
    does, and a name is not evidence.
    """
    name = get("user.name") or ""
    raw = str(get("user.pronouns", "they/them") or "they/them").lower()
    subject = raw.split("/")[0].strip()
    forms = _PRONOUNS.get(subject, _PRONOUNS["they"])
    return {
        "name": name,
        "first": name.split(" ")[0] if name else "the user",
        "subject": forms[0], "object": forms[1], "possessive": forms[2],
        "possessive_pronoun": forms[3], "reflexive": forms[4],
        "plural_verb": forms[0] == "they",
    }


def tz() -> ZoneInfo:
    return ZoneInfo(get("timezone", "America/New_York"))


def link_ledger() -> str | None:
    """Make `ROOT/ledger` point at `HOME/ledger`. Returns what it did, or None.

    This symlink is the most load-bearing thing in the layout: every prompt,
    skill and docstring refers to `ledger/identity/...`, and they are all
    relative to the checkout. Without it a session is told to read files that
    are not there.

    It existed on the first install because it was made by hand during the
    split, and **no code path created it** until a rehearsal of a fresh install
    fell over on exactly that -- which is the whole argument for doing the
    rehearsal. It is created here, from `ensure_dirs`, which every `herald`
    invocation calls, so a missing or stale link heals itself rather than
    needing to be noticed.
    """
    if LEGACY_LAYOUT:
        return None
    link = ROOT / "ledger"
    if link.is_symlink():
        try:
            if link.resolve() == LEDGER.resolve():
                return None
        except OSError:
            pass                        # dangling; replace it
        link.unlink()
        link.symlink_to(LEDGER)
        return f"repointed at {LEDGER}"
    if link.exists():
        # A real directory here in a split layout means somebody has data in
        # the wrong place. Not this function's business to move or delete it.
        return f"{link} is a real directory, not a link into {HOME}"
    link.symlink_to(LEDGER)
    return f"linked to {LEDGER}"


def ensure_dirs() -> None:
    for d in (HOME, LEDGER, IDENTITY, STATE, STATE / "areas", JOURNAL, RAW, LOGS):
        d.mkdir(parents=True, exist_ok=True)
    link_ledger()


def agent_env() -> dict:
    """Environment for a `claude` subprocess.

    Strips ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN. Claude Code ranks an API
    key above the subscription login, so a stray key exported anywhere in the
    session would silently move every Herald invocation onto metered API billing
    while appearing to work perfectly. Removing it here is the only guard.
    """
    env = dict(os.environ)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        env.pop(k, None)
    home = env.get("HOME", str(pathlib.Path.home()))
    env["PATH"] = os.pathsep.join([f"{home}/.local/bin", env.get("PATH", "/usr/bin:/bin")])
    return env
