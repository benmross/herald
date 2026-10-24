"""One fact format for tasks read from Google and tasks Herald just wrote."""

from __future__ import annotations

from . import db


def store_task(con, *, list_id: str, list_name: str, task: dict) -> None:
    deleted = bool(task.get("deleted"))
    db.put_fact(
        con, "gtasks", "task", external_id=f"{list_id}::{task['id']}",
        ts=task.get("due"), title=task.get("title") or "(untitled)",
        body=task.get("notes") or None,
        data={
            "list": list_name,
            "list_id": list_id,
            "status": "deleted" if deleted else task.get("status", "needsAction"),
            "deleted": deleted,
            "completed": task.get("completed"),
            "parent": task.get("parent"),
            "updated": task.get("updated"),
        },
    )
