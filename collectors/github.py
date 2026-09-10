#!/usr/bin/env python
"""GitHub — the user's own recent activity.

Uses the `gh` CLI that is already authenticated on this box rather than a second
token. What it is for: knowing what the user has actually been building lately, so the
agent can talk about their work without being told, and so a "what did I do this
week" answer is not purely calendar-shaped.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import collector, db  # noqa: E402

NAME = "github"
REQUIRES = ('github',)

# How often this is worth running:
# their own activity and repos
CADENCE_MINUTES = 360
EVENT_PAGES = 2


def _gh(path: str) -> list | dict:
    out = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=90)
    if out.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout or "[]")


def collect(con) -> dict:
    me = _gh("/user")["login"]
    counts = {"events": 0, "repos": 0}

    for page in range(1, EVENT_PAGES + 1):
        for ev in _gh(f"/users/{me}/events?per_page=100&page={page}"):
            payload = ev.get("payload", {})
            repo = (ev.get("repo") or {}).get("name")
            summary = ev["type"].replace("Event", "")
            if commits := payload.get("commits"):
                summary = commits[0].get("message", "").splitlines()[0][:140]
            db.put_fact(
                con, NAME, "event", external_id=ev["id"], ts=ev.get("created_at"),
                title=f"{repo}: {summary}" if repo else summary,
                data={
                    "type": ev["type"],
                    "repo": repo,
                    "ref": payload.get("ref"),
                    "commits": len(payload.get("commits") or []),
                    "action": payload.get("action"),
                    "public": ev.get("public"),
                },
            )
            counts["events"] += 1

    db.clear(con, NAME, "repo")
    for repo in _gh("/user/repos?per_page=100&sort=pushed&affiliation=owner"):
        db.put_fact(
            con, NAME, "repo", external_id=repo["full_name"],
            ts=repo.get("pushed_at"), title=repo["full_name"],
            body=repo.get("description"),
            data={
                "private": repo.get("private"),
                "language": repo.get("language"),
                "fork": repo.get("fork"),
                "stars": repo.get("stargazers_count"),
                "url": repo.get("html_url"),
            },
        )
        counts["repos"] += 1

    return counts


if __name__ == "__main__":
    collector.main(NAME, collect)
