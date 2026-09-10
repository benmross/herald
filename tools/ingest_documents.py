#!/usr/bin/env python3
"""Turn a folder of PDFs/images into facts, once.

This is not a collector. A collector reads a *live* source on a schedule and
must cost nothing per run (see `collector.py` and `docs/extending.md`). This is
the other shape of ingestion Herald keeps needing: someone hands over a folder
that already exists and will not change again -- college decision letters, a
batch of course syllabi, a scanned form -- and the job is to get its text into
`facts.db` exactly once so a session can `herald db` it instead of re-reading
files by hand every time the question comes up.

Built 7 September 2026 after doing this by hand twice in one week (course
syllabi via a Telegram document handler, then a whole college application
history via a throwaway script in /tmp) -- the second time, the ad hoc script
had already forgotten the lesson the first one learned about picking the wrong
date out of a letter's body text. Writing it down once is the point.

WHAT IT DOES, so this is never a black box:

  1. Walk `directory` recursively. For each file:
       .pdf              -> `pdftotext -layout` (keeps columns/tables readable)
       .png/.jpg/.jpeg/
       .tif/.tiff        -> `tesseract` OCR
       .txt/.md          -> read as-is, no extraction needed
     Anything else is skipped and listed at the end -- nothing is ever
     silently dropped.

  2. Extracted text is cached as a sidecar under
     `ledger/raw/ingest/<source>/<relative path>.txt` -- disposable, gitignored,
     exactly like every other cache under `raw/`. A second run reuses the
     sidecar instead of re-running pdftotext/tesseract, unless the source file
     is newer or --force is given. Read these when a fact's body looks wrong;
     that is the extraction Herald actually saw, not a guess.

  3. One `db.put_fact` call per file:
       source      = --source, e.g. "college"
       kind        = --kind, e.g. "decision" (everything in one run shares a
                     kind -- run the tool again with a different --kind for a
                     different subfolder, the way college decisions and
                     applications got two runs)
       external_id = the file's path relative to `directory`, so re-running
                     the tool on the same folder upserts instead of duplicating
       title       = "<title prefix>: <file stem>"
       ts          = the earliest date-shaped substring found anywhere in the
                     text (see `find_date` below), or None if none was found
       body        = the extracted text, capped at MAX_BODY_CHARS
       data        = {"file": absolute source path, "chars": length extracted}

  4. Prints one line per file plus a final count, and a list of any files it
     could not read (wrong extension, extraction failed, empty output) so nsomeone
     watching the run can tell "done" from "silently gave up."

WHAT IT DELIBERATELY DOES NOT DO:

  - No schedule, no cursor, no re-run cadence. If the folder can change and
    needs re-reading on its own, that is a collector, not this.
  - No judgment about what the facts mean. That happens later, once, when a
    session actually needs them -- same rule as everywhere else in Herald.
  - Date-finding is a heuristic, not a parser. It looks for the first
    "Month D, YYYY" or "M/D/YYYY" shaped substring by *position in the text*,
    not by regex priority, because the earlier college-application run picked
    a scholarship's "beginning Monday, August 24, 2026" out of a letter's body
    instead of its dateline, simply because the month-name pattern was checked
    before the slash-date pattern regardless of which came first on the page.
    It will still get this wrong sometimes -- always spot-check `ts` before
    trusting it for anything that matters, the way two of the twenty college
    decision dates needed a manual fix after the first run.

EXAMPLE:

    tools/ingest_documents.py ~/College/Decisions --source college --kind decision
    tools/ingest_documents.py ~/College/Applications --source college --kind application

Then: `herald db "select title, ts from facts where source='college' order by ts"`
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from herald import config, db  # noqa: E402

MAX_BODY_CHARS = 20_000
PDF_EXTS = {".pdf"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}
TEXT_EXTS = {".txt", ".md"}

MONTHS = ("January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December")
# One combined pattern so `finditer` finds every date-shaped substring in
# reading order; picking the first hit is what makes this "earliest on the
# page" rather than "whichever pattern happens to be tried first."
_DATE_RE = re.compile(
    r"(?P<month>" + "|".join(MONTHS) + r")\s+(?P<day>\d{1,2}),?\s+(?P<year>\d{4})"
    r"|(?P<mm>\d{1,2})/(?P<dd>\d{1,2})/(?P<yyyy>\d{4})"
)


def find_date(text: str) -> str | None:
    """The first date-shaped substring in the text, as an ISO date, or None."""
    m = _DATE_RE.search(text)
    if not m:
        return None
    try:
        if m.group("month"):
            dt = datetime.strptime(
                f"{m.group('month')} {m.group('day')} {m.group('year')}",
                "%B %d %Y")
        else:
            dt = datetime.strptime(
                f"{m.group('mm')}/{m.group('dd')}/{m.group('yyyy')}", "%m/%d/%Y")
        return dt.date().isoformat()
    except ValueError:
        return None  # e.g. "13/45/2026" matched the shape but isn't a real date


def require(tool: str) -> None:
    if shutil.which(tool) is None:
        sys.exit(f"ingest_documents: {tool!r} is not installed or not on PATH")


def extract(path: Path, cache: Path, force: bool) -> str | None:
    """Text for one file, using the cached sidecar unless it's stale."""
    ext = path.suffix.lower()
    if ext in TEXT_EXTS:
        return path.read_text(errors="ignore")

    if not force and cache.exists() and cache.stat().st_mtime >= path.stat().st_mtime:
        return cache.read_text(errors="ignore")

    cache.parent.mkdir(parents=True, exist_ok=True)
    if ext in PDF_EXTS:
        require("pdftotext")
        subprocess.run(["pdftotext", "-layout", str(path), str(cache)],
                       capture_output=True)
    elif ext in IMAGE_EXTS:
        require("tesseract")
        # tesseract appends .txt to the "output base" itself
        out_base = cache.with_suffix("")
        subprocess.run(["tesseract", str(path), str(out_base)], capture_output=True)
    else:
        return None
    return cache.read_text(errors="ignore") if cache.exists() else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("directory", type=Path, help="folder to walk (recursive)")
    ap.add_argument("--source", required=True,
                     help="facts.source, e.g. 'college'")
    ap.add_argument("--kind", default="document",
                     help="facts.kind for every file this run touches "
                          "(default: 'document')")
    ap.add_argument("--title-prefix",
                     help="default: --kind, capitalized")
    ap.add_argument("--force", action="store_true",
                     help="re-run pdftotext/tesseract even if a fresher cache exists")
    args = ap.parse_args()

    directory = args.directory.expanduser().resolve()
    if not directory.is_dir():
        sys.exit(f"ingest_documents: {directory} is not a directory")
    prefix = args.title_prefix or args.kind.capitalize()
    cache_root = config.LEDGER / "raw" / "ingest" / args.source

    files = sorted(p for p in directory.rglob("*") if p.is_file())
    con = db.connect()
    done, skipped = 0, []

    for path in files:
        rel = path.relative_to(directory).as_posix()
        cache = cache_root / (rel + ".txt")
        text = extract(path, cache, args.force)
        if not text or not text.strip():
            skipped.append(rel)
            continue

        body = text[:MAX_BODY_CHARS]
        ts = find_date(text)
        db.put_fact(con, source=args.source, kind=args.kind,
                    external_id=rel, ts=ts,
                    title=f"{prefix}: {path.stem}", body=body,
                    data={"file": str(path), "chars": len(text)})
        done += 1
        print(f"  {rel}  ts={ts or '-'}")

    con.commit()
    con.close()

    print(f"\n{done} fact(s) written as source={args.source!r} kind={args.kind!r}")
    if skipped:
        print(f"{len(skipped)} file(s) skipped (unreadable or unsupported type):")
        for rel in skipped:
            print(f"  {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
