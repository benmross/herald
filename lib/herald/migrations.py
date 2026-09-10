"""Changes an update has to make to an install that already exists.

`config/defaults.json` handles a new *setting* on its own: it is merged under
the user's config, so a key added upstream simply appears. New tables and
columns are handled by `db.SCHEMA` being `CREATE TABLE IF NOT EXISTS` and by
`db._migrate` adding columns idempotently.

What neither handles is a change of *shape*: renaming a fact kind, moving a
directory, rewriting a stored JSON field. Those happened three times in the
first two days of this program existing -- `data.course` became `data.tag`,
`state/courses/` became `state/areas/`, `ben_has_replied` became
`user_has_replied` -- and each time the fix was a one-off script run by hand on
the only install there was. That does not survive other people having copies.

So a migration is a file:

    migrations/0001_rename_course_to_tag.py

        DESCRIPTION = "data.course becomes data.tag on calendar feed facts"

        def apply(con) -> str:
            n = con.execute("UPDATE facts SET ...").rowcount
            return f"{n} rows"

`herald update` runs whatever has not run yet, in order, inside a transaction,
and records it. A fresh install marks them all as applied without running them,
because a fresh install starts at the current shape -- `stamp()`, called by
setup.

Rules for writing one, learned from the three that were done by hand:

- **Idempotent anyway.** Being recorded is the guard; being safe to run twice
  is the belt. Prefer `WHERE` clauses that no-op on a second pass.
- **Never destructive without a copy.** A migration that drops a column has to
  keep the data somewhere, because the person running it did not choose to.
- **Say what it did**, and return it. That line goes into the update's output
  and the journal, which is where somebody looks when a number is surprising.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re

from . import config, db

DIRECTORY = config.ROOT / "migrations"
_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.py$")

TABLE = """
CREATE TABLE IF NOT EXISTS applied_migrations (
    name       TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    note       TEXT
);
"""


def _ensure(con) -> None:
    con.executescript(TABLE)


def all_migrations() -> list[tuple[str, pathlib.Path]]:
    """(name, path) in order. A name that does not match the pattern is
    ignored, so notes and scratch files in the directory are harmless."""
    if not DIRECTORY.is_dir():
        return []
    found = []
    for path in sorted(DIRECTORY.iterdir()):
        if _NAME.match(path.name):
            found.append((path.stem, path))
    return found


def applied(con=None) -> set[str]:
    def _read(c) -> set[str]:
        _ensure(c)
        return {row[0] for row in c.execute("SELECT name FROM applied_migrations")}

    if con is not None:
        return _read(con)
    with db.session() as c:
        return _read(c)


def pending() -> list[tuple[str, pathlib.Path]]:
    done = applied()
    return [(name, path) for name, path in all_migrations() if name not in done]


def _load(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(f"herald_migration_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def describe(path: pathlib.Path) -> str:
    try:
        return getattr(_load(path), "DESCRIPTION", "") or path.stem
    except Exception as exc:                                        # noqa: BLE001
        return f"{path.stem} (unreadable: {type(exc).__name__})"


def run(name: str, path: pathlib.Path) -> str:
    """Apply one migration and record it. Raises if it fails, having recorded
    nothing -- a half-applied migration that claims to be done is the one
    failure mode worth going out of the way to prevent."""
    module = _load(path)
    with db.session() as con:
        _ensure(con)
        note = module.apply(con) or ""
        con.execute("INSERT OR REPLACE INTO applied_migrations "
                    "(name, applied_at, note) VALUES (?, ?, ?)",
                    (name, db.now(), str(note)[:500]))
        con.commit()
    return str(note)


def run_pending(report=None) -> list[tuple[str, str]]:
    out = []
    for name, path in pending():
        if report:
            report(f"migration {name}: {describe(path)}")
        note = run(name, path)
        out.append((name, note))
        if report and note:
            report(f"  {note}")
    return out


def stamp() -> int:
    """Record every migration as applied without running it.

    For a fresh install, whose database was created from the current schema and
    therefore already has the shape every migration exists to produce. Running
    them would be wrong as often as it was harmless.
    """
    with db.session() as con:
        _ensure(con)
        now = db.now()
        rows = [(name, now, "stamped on a fresh install")
                for name, _ in all_migrations()]
        con.executemany("INSERT OR IGNORE INTO applied_migrations "
                        "(name, applied_at, note) VALUES (?, ?, ?)", rows)
        con.commit()
    return len(rows)
