"""Keep the browser profile out of $HERALD_HOME's git history.

`herald browse` keeps a persistent Chromium profile under $HERALD_HOME/browser
so a sign-in survives. Its cookies are credentials, and $HERALD_HOME is usually
a git repository, so an install that commits with `add -A` would have pushed
them. This adds `/browser/` to that repository's .gitignore.
"""

from __future__ import annotations

from herald import config

DESCRIPTION = "ignore $HERALD_HOME/browser (the browser profile) in the ledger repo"


def apply(con) -> str:
    ignore = config.HOME / ".gitignore"
    if not (config.HOME / ".git").exists():
        return "no git repository in HERALD_HOME; nothing to do"
    text = ignore.read_text() if ignore.exists() else ""
    if any(line.strip() in ("/browser/", "browser/", "/browser") for line in text.splitlines()):
        return "already ignored"
    with ignore.open("a") as f:
        f.write(("" if text.endswith("\n") or not text else "\n")
                + "\n# The browser profile Herald signs into sites with: cookies are "
                  "credentials.\n/browser/\n")
    return "added /browser/ to .gitignore"
