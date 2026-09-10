#!/usr/bin/env python
"""Google Tasks — the user's own lists.

Small and cheap, and it is the one place the user has already written down things the user
means to do, which makes it the natural home for commitments Herald wants to
hand back to them rather than only mention.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import collector, db, google  # noqa: E402

NAME = "gtasks"
REQUIRES = ('google',)

# How often this is worth running:
# their own task lists; they do not change every half hour
CADENCE_MINUTES = 120


def collect(con) -> dict:
    svc = google.service("tasks", "v1")
    counts = {"lists": 0, "tasks": 0, "open": 0}

    db.clear(con, NAME, "list")
    lists, page = [], None
    while True:
        resp = svc.tasklists().list(maxResults=100, pageToken=page).execute()
        lists.extend(resp.get("items", []))
        page = resp.get("nextPageToken")
        if not page:
            break

    for tl in lists:
        db.put_fact(con, NAME, "list", external_id=tl["id"], title=tl.get("title"))
        counts["lists"] += 1

        page = None
        while True:
            resp = svc.tasks().list(
                tasklist=tl["id"], showCompleted=True, showHidden=True,
                maxResults=100, pageToken=page).execute()
            for t in resp.get("items", []):
                status = t.get("status", "needsAction")
                db.put_fact(
                    con, NAME, "task", external_id=f"{tl['id']}::{t['id']}",
                    ts=t.get("due"), title=t.get("title") or "(untitled)",
                    body=(t.get("notes") or "")[:1000] or None,
                    data={
                        "list": tl.get("title"),
                        "list_id": tl["id"],
                        "status": status,
                        "completed": t.get("completed"),
                        "parent": t.get("parent"),
                        "updated": t.get("updated"),
                    },
                )
                counts["tasks"] += 1
                counts["open"] += int(status == "needsAction")
            page = resp.get("nextPageToken")
            if not page:
                break

    return counts


if __name__ == "__main__":
    collector.main(NAME, collect)
