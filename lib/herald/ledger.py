"""Committing the person's own repository.

`$HERALD_HOME` is offered as a git repository at setup, with the promise that
"a bad edit to something the agent wrote about you can be undone". For the
first day of other people being able to install Herald, nothing in the program
ever made a commit there: the wizard made one when it created the directory,
and every identity file the interview wrote, every state file a cycle
rewrote, and every journal entry sat uncommitted until a session happened to
follow the instruction in a skill. A version history with one commit in it is
not a version history.

So the program commits its own work. After each setup step, and after each
cycle, whatever changed in the home directory is committed with a message that
says which step or cycle did it. If a remote is configured -- which only
happens because the person said yes to the backup question -- it is pushed
too, quietly, and a push that fails (no network, no token) is not an error.

Nothing here ever initialises a repository, adds a remote, or touches the
program's checkout. If the person said no to versioning, `commit()` is a
no-op.
"""

from __future__ import annotations

import subprocess

from . import config


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(config.HOME),
                          capture_output=True, text=True)


def versioned() -> bool:
    return (config.HOME / ".git").exists() and not config.LEGACY_LAYOUT


def commit(message: str, *, push: bool = True) -> str | None:
    """Commit everything that changed in `$HERALD_HOME`. Returns the short
    commit id, or None if there was nothing to commit or no repository."""
    if not versioned():
        return None
    _git("add", "-A")
    if not _git("status", "--porcelain").stdout.strip():
        return None
    # A repository the wizard made has no identity configured; git refuses to
    # commit without one, and asking a non-technical person to run
    # `git config user.email` is exactly the kind of thing setup exists to
    # avoid. Given per-commit rather than written into their config.
    name = config.get("agent.name", "Herald") or "Herald"
    r = _git("-c", f"user.name={name}", "-c", "user.email=herald@localhost",
             "commit", "-q", "-m", message)
    if r.returncode != 0:
        return None
    short = _git("rev-parse", "--short", "HEAD").stdout.strip()
    if push and _git("remote", "get-url", "origin").returncode == 0:
        # Fire and forget: a backup that is a little behind is fine, a cycle
        # that fails because GitHub was slow is not.
        subprocess.Popen(["git", "push", "-q", "origin", "HEAD"],
                         cwd=str(config.HOME), stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    return short
