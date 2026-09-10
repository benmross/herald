#!/usr/bin/env python
"""Make a document readable, whatever shape it arrived in.

Claude Code opens ordinary PDFs directly, so most of the time this is not
needed. It exists for the ones it cannot: a professor password-
protects every course PDF, and an encrypted PDF is opaque to the Read tool. The
password lives in config/secrets.json under documents.pdf_passwords, so new
handouts from that course just work rather than needing a person each time.

    tools/read-document.py <path>            -> text on stdout
    tools/read-document.py <path> --sidecar  -> writes <path>.txt, prints its path

Prints plain text for anything it can handle and exits non-zero, with a reason,
for anything it cannot: silence would look like an empty document.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import config  # noqa: E402

TEXTLIKE = {".txt", ".md", ".csv", ".json", ".html", ".htm"}


def pdf_text(path):
    attempts = [[]]
    for pw in (config.secret("documents.pdf_passwords", []) or []):
        attempts.append(["-upw", pw])
    last = ""
    for extra in attempts:
        proc = subprocess.run(["pdftotext", "-layout"] + extra + [str(path), "-"],
                              capture_output=True, text=True, timeout=180)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout
        last = (proc.stderr or "").strip()
    raise RuntimeError(
        "could not read " + path.name + ": " + (last or "no text extracted") +
        ". If it is password-protected, add the password to "
        "documents.pdf_passwords in config/secrets.json.")


def extract(path):
    if path.suffix.lower() == ".pdf":
        return pdf_text(path)
    if path.suffix.lower() in TEXTLIKE:
        return path.read_text(encoding="utf-8", errors="replace")
    raise RuntimeError(path.suffix + " is not a text format; open it directly.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--sidecar", action="store_true",
                    help="write <path>.txt beside the original, print its path")
    args = ap.parse_args()

    path = pathlib.Path(args.path).expanduser()
    if not path.exists():
        print("no such file: " + str(path), file=sys.stderr)
        return 2
    try:
        text = extract(path)
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1

    if args.sidecar:
        out = path.with_suffix(path.suffix + ".txt")
        out.write_text(text, encoding="utf-8")
        print(out)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
