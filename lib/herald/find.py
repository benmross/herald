"""Find files by name: the nightly locate index, minus what has gone since.

`locate` answers in milliseconds but only as of its last rebuild, usually
last night. Anything it is wrong about since then falls into two groups:

- **Names that no longer exist.** Deleted or moved since the rebuild. These
  are cheap to catch: a search returns tens of paths, and checking that each
  still exists is one `lstat` apiece.
- **Names that are new.** Almost always something the asking session made or
  moved itself, so it knows where to look. `--in DIR` adds a live search of
  just that directory, merged with the index's answer.

A live whole-filesystem index (fanotify) was built and measured as the
alternative, and rejected: it costs resident memory all day and kernel time on
every write, to fix staleness the asker can almost always fix more cheaply.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

#: Where plocate keeps its database on Debian, Ubuntu, Arch and Fedora.
DEFAULT_DB = Path("/var/lib/plocate/plocate.db")


@dataclass
class Query:
    patterns: list[str]
    ignore_case: bool = False
    regex: bool = False
    #: Match only the last component, like `locate -b`. Otherwise the whole
    #: path, like `locate`.
    basename: bool = False
    limit: int | None = None
    #: Directories to also search live, for names newer than the index.
    fresh: list[Path] = field(default_factory=list)


@dataclass
class Answer:
    paths: list[str]
    #: Paths the index had that no longer exist.
    dropped: int = 0
    #: Paths found only by the live search of `fresh` directories.
    fresh_only: int = 0
    #: Seconds since the index was rebuilt, if known.
    index_age: float | None = None


class FindError(Exception):
    pass


def index_age(db: Path = DEFAULT_DB, now: float | None = None) -> float | None:
    """Seconds since the index was last rebuilt, from the database's mtime.
    Readable even when the database itself is not."""
    try:
        return (now or time.time()) - db.stat().st_mtime
    except OSError:
        return None


def _matcher(q: Query):
    """A function deciding whether a path matches every pattern, with the
    same rules as locate: substring (or regex) against the whole path, or
    the last component with `basename`; all patterns must match (`-A`)."""
    flags = re.IGNORECASE if q.ignore_case else 0
    if q.regex:
        compiled = [re.compile(p, flags) for p in q.patterns]
        test = lambda s: all(c.search(s) for c in compiled)  # noqa: E731
    elif q.ignore_case:
        lowered = [p.casefold() for p in q.patterns]
        test = lambda s: all(p in s.casefold() for p in lowered)  # noqa: E731
    else:
        test = lambda s: all(p in s for p in q.patterns)  # noqa: E731
    if q.basename:
        return lambda path: test(os.path.basename(path.rstrip("/")))
    return test


def locate(q: Query, run=subprocess.run) -> list[str]:
    """Ask locate. `run` is a parameter so tests need no database."""
    cmd = ["locate", "-0", "-A"]
    if q.ignore_case:
        cmd.append("-i")
    if q.regex:
        cmd.append("-r")
    if q.basename:
        cmd.append("-b")
    if q.limit is not None:
        # Some results may be dropped as stale, so ask for more than needed.
        cmd += ["-l", str(q.limit * 2 + 50)]
    cmd += ["--", *q.patterns]
    try:
        proc = run(cmd, capture_output=True)
    except FileNotFoundError:
        raise FindError("locate is not installed (install plocate)") from None
    # locate exits 1 when nothing matched; that is an answer, not an error.
    if proc.returncode not in (0, 1) or (proc.returncode == 1 and proc.stderr):
        msg = proc.stderr.decode(errors="replace").strip() or f"locate exited {proc.returncode}"
        if "Permission denied" in msg:
            msg += (" (the database must be readable by the plocate group, and this "
                    "process must be in it)")
        raise FindError(msg)
    return [p.decode(errors="surrogateescape") for p in proc.stdout.split(b"\0") if p]


def still_there(paths: list[str]) -> tuple[list[str], int]:
    """Keep the paths that still exist (without following a final symlink)."""
    kept = []
    for p in paths:
        try:
            os.lstat(p)
            kept.append(p)
        except OSError:
            pass
    return kept, len(paths) - len(kept)


def search_live(q: Query) -> list[str]:
    """Walk each `fresh` directory and return the matching paths."""
    match = _matcher(q)
    out = []
    for top in q.fresh:
        top = str(Path(top).resolve())
        if match(top):
            out.append(top)
        for dirpath, dirnames, filenames in os.walk(top):
            for name in dirnames + filenames:
                p = os.path.join(dirpath, name)
                if match(p):
                    out.append(p)
    return out


def find(q: Query, run=subprocess.run, db: Path = DEFAULT_DB) -> Answer:
    if not q.patterns:
        raise FindError("give at least one pattern")
    indexed, dropped = still_there(locate(q, run))
    seen = set(indexed)
    fresh_only = [p for p in search_live(q) if p not in seen]
    paths = indexed + fresh_only
    if q.limit is not None:
        paths = paths[: q.limit]
    return Answer(paths=paths, dropped=dropped, fresh_only=len(fresh_only), index_age=index_age(db))


def describe_age(seconds: float | None) -> str:
    if seconds is None:
        return "an index of unknown age"
    hours = seconds / 3600
    if hours < 1:
        return f"an index {int(seconds // 60)} min old"
    if hours < 48:
        return f"an index {hours:.0f} h old"
    return f"an index {hours / 24:.0f} days old"
