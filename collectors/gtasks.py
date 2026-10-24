#!/usr/bin/env python
"""Google Tasks — the user's own lists.

Small and cheap, and it is the one place the user has already written down things the user
means to do, which makes it the natural home for commitments Herald wants to
hand back to them rather than only mention.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import collector, db, google, gtasks  # noqa: E402

NAME = "gtasks"
REQUIRES = ('google',)

# How often this is worth running:
# their own task lists; they do not change every half hour
CADENCE_MINUTES = 120


def collect(con) -> dict:
    svc = google.service("tasks", "v1")
    counts = {"lists": 0, "tasks": 0, "open": 0}
    previous = {row["external_id"]: row for row in con.execute(
        "SELECT * FROM facts WHERE source=? AND kind='task'", (NAME,)).fetchall()}

    lists, page = [], None
    while True:
        resp = svc.tasklists().list(maxResults=100, pageToken=page).execute()
        lists.extend(resp.get("items", []))
        page = resp.get("nextPageToken")
        if not page:
            break

    # Fetch the complete snapshot before changing facts. An API failure on a
    # later page must not mark tasks from the unread portion as deleted.
    tasks = []
    for tl in lists:
        page = None
        while True:
            resp = svc.tasks().list(
                tasklist=tl["id"], showCompleted=True, showHidden=True, showDeleted=True,
                maxResults=100, pageToken=page).execute()
            for t in resp.get("items", []):
                tasks.append((tl, t))
            page = resp.get("nextPageToken")
            if not page:
                break

    db.clear(con, NAME, "list")
    for tl in lists:
        db.put_fact(con, NAME, "list", external_id=tl["id"], title=tl.get("title"))
    counts["lists"] = len(lists)

    seen = set()
    for tl, t in tasks:
        key = f"{tl['id']}::{t['id']}"
        seen.add(key)
        if t.get("deleted") and key in previous:
            # Google may return only an id for a deleted task. Preserve the
            # last known title and notes in that case, as for absent tasks below.
            continue
        gtasks.store_task(con, list_id=tl["id"], list_name=tl.get("title"), task=t)
        if t.get("deleted"):
            continue
        counts["tasks"] += 1
        counts["open"] += int(t.get("status", "needsAction") == "needsAction")

    deleted = {f"{tl['id']}::{t['id']}" for tl, t in tasks if t.get("deleted")}
    # Only reconcile rows present before the fetch. A task just created by a
    # conversation during pagination may not be in this snapshot yet.
    for row in previous.values():
        if row["external_id"] in seen and row["external_id"] not in deleted:
            continue
        # Tasks can vanish without a tombstone, including when their entire
        # list is removed. Keep the history but stop asserting they are open.
        data = json.loads(row["data"] or "{}")
        if data.get("deleted") and data.get("status") == "deleted":
            continue
        data.update(deleted=True, status="deleted")
        db.put_fact(con, NAME, "task", external_id=row["external_id"],
                    ts=row["ts"], title=row["title"], body=row["body"], data=data)

    return counts


if __name__ == "__main__":
    collector.main(NAME, collect)
