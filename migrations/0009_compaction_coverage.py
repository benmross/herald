"""Invalidate old conversation coverage after retiring unsafe compaction."""

from herald import changes

DESCRIPTION = "warn existing readers that earlier net-zero cleanup may have hidden changes"


def apply(con) -> str:
    if con.execute("SELECT 1 FROM memory_metadata WHERE key='compaction_coverage_reset'").fetchone():
        return "earlier compaction coverage already marked"
    through = max(changes.sequence(con), changes.pruned_through(con))
    con.execute("INSERT INTO memory_metadata VALUES ('pruned_through',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(through),))
    con.execute("INSERT INTO memory_metadata VALUES ('compaction_coverage_reset',?)", (str(through),))
    return "marked earlier revision coverage incomplete; current records and retained history preserved"
