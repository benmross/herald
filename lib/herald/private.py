"""Private sessions: Herald may read anything and can save or change nothing.

A private session is asked for by name (`/private` in a Telegram topic,
`herald think --private`). For as long as it lasts, the engine and everything
it starts run inside a bubblewrap sandbox in which the whole filesystem is
mounted read-only. A prompt asking the model to write nothing would hold only
until the model forgot it. A read-only mount returns an error to whoever tries,
including the engine itself, the guard's own log, and a script the model wrote
a second ago.

What that leaves writable, and why:

    /tmp, /dev/shm   empty and private to the sandbox, gone when the turn's
                     process exits. Somewhere for a query script to live.
    the scratch      a directory on a RAM-backed filesystem, one per private
                     session, mounted over the places the engine keeps its own
                     state (transcripts, shell snapshots, history). The
                     conversation has to exist somewhere for a second message
                     to continue the first, so it exists here and is deleted
                     when the session ends. Nothing in it touches a disk.
    the login file   the engine's own directory stays writable underneath
                     those mounts for one reason: `claude` and `codex` rewrite
                     their OAuth token when it refreshes, and a refresh that
                     could not be saved would leave the real install holding a
                     token the provider has already replaced.

Everything else that a private session must not do is outside the filesystem,
and a mount cannot stop it. Those are closed at Herald's own doors, all of
which read `HERALD_PRIVATE` from the environment the sandbox sets:

- `db.connect` opens `facts.db` read-only.
- `gwrite` and `red` refuse before they reach Google or the user's phone.
- `herald` runs only its reading subcommands.
- `tools/guard.py` blocks the file-writing tools, every connector tool that is
  not a known read, and shell commands that reach another machine to change it.
  That last part reads the command text, so like the rest of the guard it stops
  the accident and the misled session. It cannot stop a determined one.

Herald's own bookkeeping stays out as well: no `runs` row, no anomaly log, no
Telegram session record, and the bridge keeps a private session's pointer in
memory only.

Standard library only, like `safety.py`: the guard reads `ENV` by name and the
bridge imports this before every private turn.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import tempfile

ENV = "HERALD_PRIVATE"

#: Where each engine keeps its state, and the variable that moves it.
ENGINE_HOMES = {"claude": ("CLAUDE_CONFIG_DIR", "~/.claude"),
                "codex": ("CODEX_HOME", "~/.codex")}
#: The login. Left on the real directory so a token refresh is not lost.
CREDENTIALS = {".credentials.json", "auth.json"}
#: Configuration the engine reads and a session has no reason to change.
CONFIG = {"settings.json", "settings.local.json", "keybindings.json", "skills",
          "plugins", "hooks", "agents", "commands", "output-styles", "rules",
          "config.toml", "hooks.json", "prompts", "packages"}
#: Files SQLite creates beside a database. Codex keeps several databases at the
#: top of its directory, which has to stay writable for the login, so each of
#: these is given a place in the scratch before the engine can create a real one.
SIDECARS = ("-wal", "-shm")

PROMPT = (
    "This is a private session. The user asked for it, and it changes the rules "
    "you were given about memory. The whole filesystem is mounted read-only for "
    "you and everything you run, the ledger and facts.db included, and nothing "
    "about this conversation is recorded anywhere: when it ends, its transcript "
    "is deleted. Read whatever helps you answer. Write nothing: no journal "
    "entry, no ledger or state update, no memory file, no obligation, deadline, "
    "calendar event, draft, label or task, no commit, and nothing on another "
    "machine or service. The instruction to write down what you learn before "
    "the conversation ends does not apply here. /tmp is writable if a query "
    "needs a scratch file, and it is gone when the turn ends. If the user asks "
    "for something that needs a write, say that it cannot be done in a private "
    "session and that `/private off` ends it.")


def active() -> bool:
    """Is this process inside a private session?"""
    return os.environ.get(ENV) == "1"


class Refused(RuntimeError):
    """Something tried to write from inside a private session."""


def refuse(what: str) -> None:
    """Raise if this process is in a private session. Called at each door."""
    if active():
        raise Refused(f"{what} is not available in a private session, which can "
                      f"read and cannot save or change anything.")


def unavailable() -> str | None:
    """Why a private session cannot start here, or None if it can."""
    if not shutil.which("bwrap"):
        return ("Private sessions need bubblewrap (`bwrap`), which mounts the "
                "filesystem read-only for the session. It is not installed here.")
    if _ram_root() is None:
        return ("Private sessions keep their transcript on a RAM-backed "
                "filesystem, and neither $XDG_RUNTIME_DIR nor /dev/shm is "
                "usable here.")
    return None


def _ram_root() -> pathlib.Path | None:
    for base in (os.environ.get("XDG_RUNTIME_DIR"), "/dev/shm"):
        if base and os.path.isdir(base) and os.access(base, os.W_OK):
            return pathlib.Path(base) / f"herald-private-{os.getuid()}"
    return None


def new_scratch(owner: str = "once") -> pathlib.Path:
    """A fresh RAM-backed directory for one private session. `owner` names
    what holds its pointer, so `sweep` can clear one owner's and no other's."""
    root = _ram_root()
    if root is None:
        raise Refused(unavailable() or "no RAM-backed directory")
    root.mkdir(mode=0o700, exist_ok=True)
    return pathlib.Path(tempfile.mkdtemp(prefix=f"{owner}-", dir=root))


def destroy(scratch) -> None:
    """End a private session: its transcript and attachments are gone."""
    root = _ram_root()
    path = pathlib.Path(scratch)
    if root is not None and path.parent == root:
        shutil.rmtree(path, ignore_errors=True)


def sweep(owner: str) -> int:
    """Delete every scratch directory `owner` made. Run when the bridge
    starts: a private session's pointer lived only in the memory of the
    process that died."""
    root = _ram_root()
    if root is None or not root.is_dir():
        return 0
    found = [p for p in root.iterdir() if p.is_dir() and p.name.startswith(f"{owner}-")]
    for p in found:
        shutil.rmtree(p, ignore_errors=True)
    return len(found)


def engine_home(engine: str, env=None) -> pathlib.Path:
    var, default = ENGINE_HOMES[engine]
    env = os.environ if env is None else env
    return pathlib.Path(env.get(var) or default).expanduser()


def _state_mounts(engine: str, scratch: pathlib.Path, env=None,
                  program: str | None = None) -> list[str]:
    """bwrap arguments that keep the engine's own state off the disk.

    The engine's directory is bound writable so the login can refresh, and
    then every entry in it except the login is covered: configuration is bound
    read-only, a directory is replaced by an empty one in the scratch, and any
    other file by a copy there. The copies are made once per session, so a
    second turn finds what the first one wrote.

    `program` is the engine's executable. Codex installs itself inside its own
    state directory, and an empty directory mounted over the entry that holds
    the binary leaves nothing to run, so that entry is treated as configuration
    whatever it is called.
    """
    home = engine_home(engine, env)
    if not home.is_dir():
        return []
    args = ["--bind", str(home), str(home)]
    store = scratch / engine
    store.mkdir(mode=0o700, exist_ok=True)
    found = shutil.which(program, path=(env or os.environ).get("PATH")) if program else None
    binary = pathlib.Path(found).resolve() if found else None
    entries = {e.name: e for e in home.iterdir()}
    for name in list(entries):
        if name.endswith((".sqlite", ".db")):
            for suffix in SIDECARS:
                entries.setdefault(name + suffix, home / (name + suffix))
    for name, entry in sorted(entries.items()):
        # bwrap cannot mount onto a symlink. Its target is on the read-only
        # root already.
        if name in CREDENTIALS or entry.is_symlink():
            continue
        if name in CONFIG or (binary is not None and entry.resolve() in binary.parents):
            args += ["--ro-bind", str(entry), str(entry)]
            continue
        shadow = store / name
        if entry.is_dir():
            shadow.mkdir(mode=0o700, exist_ok=True)
        elif entry.is_file():
            if not shadow.exists():
                try:
                    shutil.copy2(entry, shadow)
                except OSError:
                    continue
        elif not entry.exists():
            shadow.touch(mode=0o600, exist_ok=True)   # a sidecar not yet made
        else:
            continue
        args += ["--bind", str(shadow), str(entry)]
    return args


def wrap(cmd: list[str], engine: str, scratch, env=None) -> list[str]:
    """`cmd`, run inside the read-only sandbox for this private session."""
    scratch = pathlib.Path(scratch)
    return ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--tmpfs", "/tmp", "--tmpfs", "/var/tmp",
            *_state_mounts(engine, scratch, env, cmd[0] if cmd else None),
            "--setenv", ENV, "1", "--setenv", "TMPDIR", "/tmp",
            "--die-with-parent", "--", *cmd]
