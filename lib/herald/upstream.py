"""Where this install's program comes from, and what it may do to it.

Herald is one program with one author and, from 10 September 2026, other
people's copies of it. That makes a question every install has to answer:
**when the user asks it to change itself, does it change itself, or does it
change what it is allowed to change and leave the program alone?**

Three answers, in `mode`:

`tracking` (the default, and what the installer offers first)
    The program is not this install's to edit. It follows tagged releases from
    upstream: `herald update` fast-forwards to the newest tag, runs any
    migrations, and reconciles. Everything personal is still fully editable,
    because everything personal is somewhere else -- the config, the ledger,
    and extensions in `$HERALD_HOME`, which can override a bundled extension by
    name. A tracking install that wants a program change asks upstream for it.

`fork`
    The program is theirs. It may edit itself, and it will not receive updates,
    because a fast-forward over local commits is not a thing. `herald update`
    says so and offers to show what upstream has done instead.

`maintainer`
    This install *is* upstream. It edits itself, commits, and pushes -- and one
    condition holds absolutely: nothing personal may go into the program. That
    is not a matter of care; `herald check` decides it, and a push that has not
    passed it has no business happening. A release is a separate, deliberate
    act (`herald release`), so work in progress on `main` reaches nobody.

Why tagged releases rather than `main`: the maintainer's own Herald pushes to
`main` as it works, several times an hour on a busy day. If installs followed
`main`, every half-finished commit would be somebody else's agent. A tag is the
point at which a change is meant for other people.

Why local `main` still moves to the tag rather than checking the tag out: a
detached HEAD is a confusing thing to hand a non-technical person, and
`git merge --ff-only <tag>` leaves them on `main`, at the released commit, able
to fast-forward again next time.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess

from . import config

MODES = ("tracking", "fork", "maintainer")
DEFAULT_MODE = "tracking"

#: Written by `herald release`, read by `herald update` to say what is coming.
CHANGELOG = config.ROOT / "CHANGELOG.md"

_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def mode() -> str:
    """This install's relationship to the program it is running.

    Unset means tracking. That is the safe default in both directions: a
    tracking install that should have been a fork loses nothing but the ability
    to self-edit until someone changes one setting, while a fork that should
    have been tracking would silently stop receiving fixes.
    """
    value = str(config.get("mode", DEFAULT_MODE) or DEFAULT_MODE).lower()
    return value if value in MODES else DEFAULT_MODE


def is_tracking() -> bool:
    return mode() == "tracking"


def may_self_edit() -> bool:
    return mode() in ("fork", "maintainer")


def _git(*args: str, cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd or config.ROOT),
                          capture_output=True, text=True)


def version() -> str:
    from . import __version__  # noqa: PLC0415
    return __version__


def current_commit() -> str:
    return _git("rev-parse", "HEAD").stdout.strip()[:12]


def _sort_key(tag: str) -> tuple:
    m = _TAG.match(tag)
    return tuple(int(p) for p in m.groups()) if m else (0, 0, 0)


def tags(remote: bool = False) -> list[str]:
    """Release tags, oldest first. Only `vN.N.N` counts as a release."""
    if remote:
        out = _git("ls-remote", "--tags", "origin").stdout
        found = {line.rsplit("/", 1)[-1] for line in out.splitlines()
                 if "refs/tags/" in line and not line.endswith("^{}")}
    else:
        found = set(_git("tag", "--list").stdout.split())
    return sorted((t for t in found if _TAG.match(t)), key=_sort_key)


def current_release() -> str | None:
    """The newest release tag that is an ancestor of HEAD."""
    out = _git("describe", "--tags", "--abbrev=0", "--match", "v*").stdout.strip()
    return out or None


def local_changes() -> dict:
    """What this checkout has that upstream does not: edits, and commits.

    In tracking mode either of those is a problem, because an update is a
    fast-forward and a fast-forward cannot happen over them. Reported rather
    than guessed at, so the message can say which files.
    """
    dirty = [line[3:] for line in _git("status", "--porcelain").stdout.splitlines()
             if line and not line.startswith("??")]
    untracked = [line[3:] for line in _git("status", "--porcelain").stdout.splitlines()
                 if line.startswith("??")]
    ahead = _git("log", "--oneline", "@{upstream}..HEAD").stdout.split("\n")
    ahead = [line for line in ahead if line.strip()]
    return {"modified": dirty, "untracked": untracked, "commits": ahead}


def fetch() -> tuple[bool, str]:
    r = _git("fetch", "--tags", "--quiet", "origin")
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()[:300]
    return True, ""


def available() -> dict | None:
    """The release this install could move to, or None if it is current.

    Reads what has already been fetched; call `fetch()` first for a fresh
    answer. Returns the tag, how many commits it is ahead, and the changelog
    section for it if there is one.
    """
    here = current_release()
    remote = tags(remote=False)          # after a fetch, tags are local
    newer = [t for t in remote if _sort_key(t) > _sort_key(here or "v0.0.0")]
    if not newer:
        return None
    target = newer[-1]
    if not _is_descendant(target):
        # A tag that is not ahead of us is not an update -- it is a different
        # history, which means somebody rewrote it or this is a fork.
        return None
    log = _git("log", "--oneline", f"HEAD..{target}").stdout.strip().splitlines()
    if not log:
        # A higher version number on the commit already checked out. `herald
        # release` always commits (the version bump and the changelog entry), so
        # this only happens for a tag made by hand -- and fast-forwarding to
        # where you already are, then announcing an update, is worse than
        # saying nothing.
        return None
    return {"tag": target, "from": here, "commits": log,
            "notes": changelog_section(target)}


def _is_descendant(tag: str) -> bool:
    return _git("merge-base", "--is-ancestor", "HEAD", tag).returncode == 0


def changelog_section(tag: str) -> str:
    """The CHANGELOG entry for one release, if the file has one."""
    if not CHANGELOG.exists():
        return ""
    text = CHANGELOG.read_text()
    match = re.search(rf"^##\s+{re.escape(tag)}\b.*?$(.*?)(?=^##\s+v|\Z)",
                      text, re.M | re.S)
    return match.group(1).strip() if match else ""


def unreleased() -> list[str]:
    """Commits on this branch since the last release tag.

    What `herald release` would ship, and what the maintainer's own status
    output reports so that "how much is waiting" is never a guess.
    """
    here = current_release()
    spec = f"{here}..HEAD" if here else "HEAD"
    return [line for line in _git("log", "--oneline", spec).stdout.splitlines()
            if line.strip()]


# --------------------------------------------------------------------------
# The git hook that makes tracking mode real.
#
# A rule in a prompt is most of the work, and `herald check` makes drift
# visible -- but a session that edits and commits anyway leaves an install that
# can never fast-forward again, and the person running it finds out weeks later
# when an update fails. Hence the hook: in tracking mode a commit in
# the program's repository is refused by git itself.
# --------------------------------------------------------------------------

HOOK_MARKER = "# herald-tracking-mode"
HOOK = f"""#!/bin/sh
{HOOK_MARKER}
# This Herald tracks upstream releases, so its program directory is not its own
# to change: an update is a fast-forward, and a fast-forward cannot happen over
# a local commit. Everything personal lives in $HERALD_HOME and is yours to
# edit freely -- config, the ledger, and extensions, which can override a
# bundled one by name.
#
# If you want a change to Herald itself, either ask upstream for it, or switch
# this install to a fork and keep it:
#
#     herald mode fork      # self-editing, no more updates
#
# Removing this hook is not the way; `herald check` reports the drift either
# way, and the next update will refuse.
echo "herald: this install tracks upstream releases, so commits here are refused." >&2
echo "herald: run 'herald mode fork' to take ownership of the program, or make" >&2
echo "herald: the change in \\$HERALD_HOME (config, ledger, extensions) instead." >&2
exit 1
"""


def hook_path() -> pathlib.Path:
    out = _git("rev-parse", "--git-path", "hooks").stdout.strip()
    base = pathlib.Path(out)
    if not base.is_absolute():
        base = config.ROOT / base
    return base / "pre-commit"


def hook_installed() -> bool:
    path = hook_path()
    return path.exists() and HOOK_MARKER in path.read_text()


def install_hook() -> bool:
    """Refuse commits in the program repo. Returns whether it changed anything."""
    path = hook_path()
    if path.exists() and HOOK_MARKER not in path.read_text():
        # Somebody else's hook. Leave it; say so rather than clobbering it.
        raise FileExistsError(f"{path} already exists and is not Herald's")
    if hook_installed():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HOOK)
    path.chmod(0o755)
    return True


def remove_hook() -> bool:
    path = hook_path()
    if hook_installed():
        path.unlink()
        return True
    return False


def sync_hook() -> str:
    """Make the hook match the mode. Called after any mode change."""
    if is_tracking():
        try:
            return "installed" if install_hook() else "already installed"
        except FileExistsError as exc:
            return f"not installed: {exc}"
    return "removed" if remove_hook() else "not needed"


def set_mode(new: str) -> str:
    if new not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    config.set_user("mode", new)
    return sync_hook()


# --------------------------------------------------------------------------
# Applying an update.
#
# A fast-forward is the easy part. What makes an update *smooth* is everything
# that has to happen afterwards on a machine that was already running: new
# dependencies, migrations, a regenerated constitution, re-linked extension
# skills, changed unit files, and a restart that does not kill a conversation
# mid-sentence. Each of those has been a separate manual step at some point in
# this program's short life, which is exactly why they belong in one function.
# --------------------------------------------------------------------------

def reconcile(report=None) -> list[str]:
    """Bring a running install into line with the code now on disk."""
    from . import constitution, extensions, migrations  # noqa: PLC0415

    def say(line: str) -> None:
        if report:
            report(line)

    done = []

    requirements = config.ROOT / "requirements.txt"
    if requirements.exists():
        r = subprocess.run([config.python(), "-m", "pip", "install", "--quiet",
                            "-r", str(requirements)], capture_output=True, text=True)
        if r.returncode == 0:
            done.append("dependencies satisfied")
        else:
            done.append(f"pip failed: {(r.stderr or '').strip()[-200:]}")
        say(done[-1])

    for name, note in migrations.run_pending(report=say):
        done.append(f"migration {name}: {note or 'applied'}")

    if constitution.write():
        done.append("CLAUDE.md regenerated")
        say(done[-1])

    sync = extensions.sync()
    if sync.get("skills_linked") or sync.get("skills_removed"):
        done.append(f"skills relinked ({len(sync.get('skills_linked', []))} in, "
                    f"{len(sync.get('skills_removed', []))} out)")
        say(done[-1])

    try:
        from setup import services  # noqa: PLC0415
        import sys as _sys
        if str(config.ROOT) not in _sys.path:
            _sys.path.insert(0, str(config.ROOT))
        written = services.install(force=True).get("written") or []
        if written:
            done.append(f"background jobs updated: {', '.join(written)}")
            say(done[-1])
    except Exception as exc:                                        # noqa: BLE001
        done.append(f"could not refresh the background jobs: {type(exc).__name__}")
        say(done[-1])

    return done


def check_invariants() -> tuple[bool, str]:
    """Run tools/check.py. An update that breaks the architecture should be
    visible immediately rather than at 06:30 tomorrow."""
    r = subprocess.run([config.python(), str(config.ROOT / "tools" / "check.py")],
                       capture_output=True, text=True, cwd=str(config.ROOT))
    return r.returncode == 0, (r.stdout or "")[-1200:]


def apply(target: str, report=None) -> dict:
    """Fast-forward to a release tag and reconcile. Returns what happened."""
    def say(line: str) -> None:
        if report:
            report(line)

    before = current_release() or current_commit()
    local = local_changes()
    if local["modified"] or local["commits"]:
        return {"ok": False,
                "error": "this checkout has local changes, so it cannot "
                         "fast-forward",
                "detail": local}

    r = _git("merge", "--ff-only", target)
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or r.stdout).strip()[:400]}
    say(f"moved from {before} to {target}")

    steps = reconcile(report=say)
    ok, output = check_invariants()
    return {"ok": True, "from": before, "to": target, "steps": steps,
            "checked": ok, "check_output": output}


def restart_services(report=None) -> str:
    """Restart what is running, without killing a live conversation.

    `herald restart --defer` waits for the Telegram bridge to be idle. The
    brain has no idle signal, so it is restarted directly -- a session there is
    resumable, where a Telegram turn mid-answer is not.
    """
    r = subprocess.run([str(config.ROOT / "bin" / "herald"), "restart", "--defer",
                        "--reason", "update applied", "herald-telegram.service"],
                       capture_output=True, text=True)
    out = (r.stdout or "").strip()
    b = subprocess.run(["systemctl", "--user", "restart", "herald-brain.service"],
                       capture_output=True, text=True)
    if report:
        report(out or "restart requested")
    return out + ("" if b.returncode == 0 else f" (brain: {b.stderr.strip()[:120]})")


# --------------------------------------------------------------------------
# Cutting a release, which is the maintainer's side of the same mechanism.
# --------------------------------------------------------------------------

def next_version(bump: str = "patch") -> str:
    here = current_release() or "v0.0.0"
    major, minor, patch = _sort_key(here)
    if bump == "major":
        major, minor, patch = major + 1, 0, 0
    elif bump == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    return f"v{major}.{minor}.{patch}"


def write_changelog(tag: str, commits: list[str]) -> None:
    """Prepend a section for this release.

    The commit subjects are the entry. This project's commit messages are
    written to explain a decision rather than to name a diff, so they are
    already the changelog -- and a hand-written summary that drifts from them
    is worse than none.
    """
    import datetime as dt  # noqa: PLC0415
    header = f"## {tag} — {dt.date.today():%-d %B %Y}\n\n"
    body = "\n".join(f"- {line.split(' ', 1)[-1]}" for line in commits) + "\n"
    intro = ("# Changelog\n\nWhat each release changed, from the commit "
             "messages. `herald update` shows the entry for whatever it is "
             "about to install.\n\n")
    if CHANGELOG.exists():
        existing = CHANGELOG.read_text()
        existing = existing.split("\n\n", 2)[-1] if existing.startswith("# Changelog") else existing
        CHANGELOG.write_text(intro + header + body + "\n" + existing.lstrip())
    else:
        CHANGELOG.write_text(intro + header + body)


def set_version(new: str) -> None:
    path = config.ROOT / "lib" / "herald" / "__init__.py"
    text = path.read_text()
    path.write_text(re.sub(r'__version__ = "[^"]*"',
                           f'__version__ = "{new.lstrip("v")}"', text))
