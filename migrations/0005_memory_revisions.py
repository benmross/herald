"""Introduce revision tracking without treating old rows as newly discovered."""

from herald import changes

DESCRIPTION = "track substantive ledger revisions and distinguish complete from partial sync"


def apply(con) -> str:
    # Also installed by connect(): old last_ok values cannot establish that
    # historical runs were complete, so never manufacture last_complete dates.
    changes.scan_documents(con)
    return "revision tracking installed; legacy sync completeness remains unknown"
