"""How ledger rows reach a session: whole, or with a pointer to the whole.

Written 14 Sep 2026, after a one-line question about a class took eight model
round trips instead of three. `herald db` cut every column to 60 characters and
said nothing about it, so a mail body came back as its first line; the session
went looking for the rest, found no `sqlite3` binary, fell back to Python, and
then discovered the ledger only held Gmail's snippet anyway. Every one of those
steps was a round trip, and round trips are where a turn's time goes.

Two rules replace the silent cut.

**Nothing is shortened without saying so and saying where the rest is.** A
value is printed whole. If the output as a whole would pass the budget, it stops
at a row boundary and names what gets the remainder. A single value too big for
any budget is written to a file whose path is printed, so the Read tool can page
through the original instead of a session re-querying for pieces of it.

**The budget exists because the window is finite, not to save tokens.** Claude
Code's Bash tool already cuts output past roughly 30,000 characters, from the
middle and without a pointer. Stopping a little below that, at a boundary, with
a note, is strictly better than letting that happen.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from typing import Mapping, Sequence

BUDGET = 24_000        # below the Bash tool's own ~30k cut, with room for a note
TABLE_CELL = 60        # anything wider, or multi-line, switches to records
SPILL_PREVIEW = 1_500  # how much of a spilled value is shown inline


def _text(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return str(value)


def _pretty(col: str, value) -> str:
    """`data` is JSON; indented, a nested value can actually be read."""
    if col == "data" and isinstance(value, str) and value[:1] in "{[":
        try:
            return json.dumps(json.loads(value), indent=2, ensure_ascii=False)
        except ValueError:
            pass
    return _text(value)


def _spill(text: str, spill_dir: pathlib.Path, name: str) -> pathlib.Path:
    spill_dir.mkdir(parents=True, exist_ok=True)
    path = spill_dir / (re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") + ".txt")
    path.write_text(text)
    return path


def _spilled(text: str, spill_dir: pathlib.Path | None, name: str) -> str:
    if spill_dir is None:
        return text
    path = _spill(text, spill_dir, name)
    return (f"{text[:SPILL_PREVIEW]}\n[... that is the first {SPILL_PREVIEW:,} of "
            f"{len(text):,} characters. The whole value is in {path} -- Read it, "
            f"with offset and limit if it is long.]")


def _name_for(row: Mapping, cols: Sequence[str], col: str, text: str) -> str:
    if "id" in cols and row["id"] is not None:
        return f"fact-{row['id']}-{col}"
    return f"value-{hashlib.sha1(text.encode()).hexdigest()[:12]}"


def _stopped(shown: int, total: int, budget: int) -> str:
    return (f"[stopped after {shown} of {total} rows to stay under {budget:,} "
            f"characters. Select fewer columns, page with LIMIT/OFFSET, or use "
            f"`herald fact <id> ...` for particular rows whole. --budget 0 lifts "
            f"the limit.]")


def render(rows: Sequence[Mapping], *, budget: int = BUDGET,
           spill_dir: pathlib.Path | None = None) -> str:
    """Rows as text a session can use in one read.

    - one row of one column prints the bare value, so `$(herald db ...)` works
    - narrow single-line results print as a table
    - anything wider prints as records, every value whole
    """
    if not rows:
        return "(no rows)"
    cols = list(rows[0].keys())

    if len(rows) == 1 and len(cols) == 1:
        value = _pretty(cols[0], rows[0][cols[0]])
        if budget and len(value) > budget:
            return _spilled(value, spill_dir, _name_for(rows[0], cols, cols[0], value))
        return value

    cells = [[_pretty(c, r[c]) for c in cols] for r in rows]
    wide = any(len(v) > TABLE_CELL or "\n" in v for row in cells for v in row)
    out: list[str] = []
    used = 0

    def fits(block: str) -> bool:
        return not budget or used + len(block) + 1 <= budget

    if not wide:
        widths = [max(len(c), *(len(row[i]) for row in cells))
                  for i, c in enumerate(cols)]
        out.append("  ".join(c.ljust(w) for c, w in zip(cols, widths)).rstrip())
        used = len(out[0]) + 1
        for n, row in enumerate(cells):
            line = "  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip()
            if not fits(line):
                out.append(_stopped(n, len(rows), budget))
                return "\n".join(out)
            out.append(line)
            used += len(line) + 1
        out.append(f"({len(rows)} row{'' if len(rows) == 1 else 's'})")
        return "\n".join(out)

    for n, (row, vals) in enumerate(zip(rows, cells)):
        block = _record(n, len(rows), cols, vals)
        if not fits(block):
            if n:
                out.append(_stopped(n, len(rows), budget))
                return "\n".join(out)
            # The very first row is too big by itself: there is no smaller
            # boundary to stop at, so its largest values go to files instead.
            vals = [_spilled(v, spill_dir, _name_for(row, cols, c, v))
                    if len(v) > SPILL_PREVIEW and spill_dir is not None else v
                    for c, v in zip(cols, vals)]
            block = _record(n, len(rows), cols, vals)
        out.append(block)
        used += len(block) + 1
    return "\n".join(out)


def _record(n: int, total: int, cols: Sequence[str], vals: Sequence[str]) -> str:
    lines = [f"--- row {n + 1} of {total} ---"]
    for c, v in zip(cols, vals):
        if "\n" in v or len(v) > TABLE_CELL:
            lines.append(f"{c}:\n{v}")
        else:
            lines.append(f"{c}: {v}")
    return "\n".join(lines)
