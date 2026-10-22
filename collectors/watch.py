#!/usr/bin/env python
"""Recheck watched pages until what they are about opens. See lib/herald/watch.py.

Renders each page in a fresh headless browser, because the pages worth
watching (career sites, programme pages) are mostly built by JavaScript and a
plain fetch sees nothing. A match on `watch_for` is pushed at once; any other
change is a fact the scout cycle judges.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import browse, collector, notify, watch  # noqa: E402

NAME = "watch"
REQUIRES = ()

# Four times a day. The programme that prompted this was open for fourteen
# days; six hours of delay is a small share of that, and a page render is
# about ten seconds each.
CADENCE_MINUTES = 360


def collect(con) -> dict:
    counts = {"checked": 0, "hit": 0, "changed": 0, "error": 0}
    for row in watch.due(con):
        r = watch.check(con, row, browse.fetch_text)
        con.commit()
        counts["checked"] += 1
        if r["event"] in counts:
            counts[r["event"]] += 1
        if r["event"] == "hit":
            notify.tell(
                f"{r['org'] or r['title']} looks open",
                f"{r['title']}: the page you were waiting on now says\n\n"
                f"\"{r['excerpt']}\"\n\n{r['url']}")
    return counts


if __name__ == "__main__":
    collector.main(NAME, collect)
