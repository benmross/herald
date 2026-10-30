"""Keep the rebuildable search index out of $HERALD_HOME's git history."""

from __future__ import annotations

from herald import config

DESCRIPTION = "ignore ledger/search.db in the ledger repo"


def apply(con) -> str:
    ignore = config.HOME / ".gitignore"
    if not (config.HOME / ".git").exists():
        return "no git repository in HERALD_HOME; nothing to do"
    text = ignore.read_text() if ignore.exists() else ""
    if "ledger/search.db" in text:
        return "already ignored"
    with ignore.open("a") as f:
        f.write(("" if text.endswith("\n") or not text else "\n")
                + "\n# Rebuilt from the rest of the ledger.\nledger/search.db\nledger/search.db-journal\n")
    return "added ledger/search.db to .gitignore"
