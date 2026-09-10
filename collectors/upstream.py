#!/usr/bin/env python
"""Notice when a new release of Herald exists, and offer it.

A tracking install that nobody ever updates drifts arbitrarily far behind and
finds out about it when something unrelated breaks. So this checks once a day,
and when there is a release it does two things: records it as a fact, so the
morning digest can mention it, and asks -- once -- through the approvals
mechanism, so saying yes is one tap on a phone rather than a command in a
terminal somebody may never open.

**Detection is here; application is not.** Applying an update restarts the very
services this collector runs under, so it is spawned as a detached process that
outlives this one. A collector that restarted itself half way through a git
fast-forward would be a genuinely bad way to find out about this.

Three things it deliberately does not do:

- **Nothing on a fork or a maintainer install.** Neither takes updates, and a
  maintainer's own unreleased commits are not news about somebody else's
  release.
- **No auto-apply.** Deliberate: one bad push should reach
  nobody who did not agree to it, which also means the first person to say "it
  broke" is one person rather than everybody at once.
- **No re-asking.** One pending approval per release. An unanswered question is
  not a reason to ask again tomorrow, and the digest carries it anyway.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import approvals, collector, config, db, notify, upstream  # noqa: E402

NAME = "upstream"
REQUIRES = ()

# How often this is worth running: a release is not urgent, and asking git to
# talk to a remote 48 times a day to learn nothing is exactly the waste every
# cadence in this program exists to avoid.
CADENCE_MINUTES = 1440

KIND = "herald.update"


def _pending_for(tag: str) -> dict | None:
    for row in approvals.pending():
        if row["kind"] == KIND and row["target"] == tag:
            return row
    return None


def _approved_for(tag: str) -> dict | None:
    rows = db.query(
        "SELECT * FROM approvals WHERE kind = ? AND target = ? AND state = ?"
        " ORDER BY id DESC LIMIT 1", (KIND, tag, approvals.APPROVED))
    return dict(rows[0]) if rows else None


def _spawn_update() -> int:
    """Run the update in a process that survives this one.

    `start_new_session` detaches it from the collector's process group, so the
    service restart the update performs cannot take the updater down with it.
    """
    log = config.LOGS / "update.log"
    config.LOGS.mkdir(parents=True, exist_ok=True)
    handle = log.open("a")
    proc = subprocess.Popen(
        [str(config.ROOT / "bin" / "herald"), "update"],
        cwd=str(config.ROOT), stdout=handle, stderr=handle,
        start_new_session=True, env={**os.environ, "HERALD_UNATTENDED": "1"})
    return proc.pid


def collect(con) -> dict:
    mode = upstream.mode()
    if mode != "tracking":
        db.clear(con, NAME, "release")
        return {"skipped": f"{mode} installs do not take updates"}

    ok, err = upstream.fetch()
    if not ok:
        # Not a failure. A laptop is offline half the time, and a missed daily
        # check for a release costs nothing -- while raising here would start a
        # failure streak and tell the user "upstream is failing", which is both
        # alarming and wrong. The next pass tries again.
        return {"upstream unreachable": 1}

    release = upstream.available()
    db.clear(con, NAME, "release")
    if not release:
        return {"up to date": 1}

    tag = release["tag"]
    db.put_fact(
        con, NAME, "release", external_id=tag, ts=db.now(),
        title=f"Herald {tag} is available",
        body=(release["notes"] or "\n".join(release["commits"][:10]))[:2000],
        data={"tag": tag, "from": release["from"],
              "commits": len(release["commits"])},
    )

    # Already said yes? Then the only thing left is to do it, out of process.
    if approved := _approved_for(tag):
        pid = _spawn_update()
        approvals.mark_done(approved["id"])
        return {"applying": tag, "pid": pid}

    if _pending_for(tag):
        return {"awaiting your answer": tag}

    approval_id = approvals.request(
        actor=f"collector:{NAME}", kind=KIND, target=tag,
        summary=f"Update Herald to {tag}",
        payload={"tag": tag, "from": release["from"]})
    notify.ask(
        approval_id,
        f"Herald {tag} is available"
        + (f" (you are on {release['from']})" if release["from"] else "")
        + ".\n\n"
        + (release["notes"] or "\n".join(release["commits"][:8]))[:1200]
        + "\n\nIt fast-forwards, runs any migrations, and restarts. Nothing "
          "you have written is touched.",
        yes="Update", no="Not now")
    return {"offered": tag}


if __name__ == "__main__":
    collector.main(NAME, collect)
