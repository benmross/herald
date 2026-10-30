"""Remove revisions that record rebuilds, not changes."""

from herald import changes

DESCRIPTION = "drop change history that nets to nothing, written before compaction existed"


def apply(con) -> str:
    return f"removed {changes.compact(con)} no-op revisions; run VACUUM to return the space"
