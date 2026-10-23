#!/usr/bin/env python
"""Move applications to submitted or rejected from the mail they cause.

Herald fills applications and the person submits them, so Herald never sees
the submit. The confirmation mail ("Thank you for applying", "we have
received your application") is the record that it happened, and the
rejection mail is the record that it ended. This reads mail already in
facts.db, matches it to open applications by company name, and moves each
row forward. No model is involved, and nothing is sent.

Matching is by company name in the sender, subject or opening of the body,
after the application was created. A confirmation only ever moves a row
forward (filling or filled to submitted); a rejection ends it.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import apply as ap  # noqa: E402
from herald import collector, db  # noqa: E402

NAME = "applications"
REQUIRES = ()
CADENCE_MINUTES = 60

SUBMITTED = re.compile(
    r"thank(s| you) for (applying|your application|your interest in)|"
    r"(we('ve| have)|has been) received your application|application (has been |was )?(received|submitted)|"
    r"we received your application|your application to .{0,60} (has been|was) (received|submitted)", re.I)
REJECTED = re.compile(
    r"unfortunately|not (to )?(move|moving) forward|will not be (moving|proceeding)|"
    r"decided to (pursue|move forward with) other|position has been filled|"
    r"no longer (being )?considered|not been selected|not selected", re.I)


def squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def collect(con) -> dict:
    counts = {"submitted": 0, "rejected": 0}
    marks = ",".join("?" * len(ap.DONE[1:]))
    apps = con.execute(f"SELECT * FROM applications WHERE status NOT IN ({marks})", ap.DONE[1:]).fetchall()
    if not apps:
        return counts
    since = min(a["created_at"] for a in apps)
    mail = con.execute("""SELECT id, ts, title, body, data FROM facts
                          WHERE source='gmail' AND kind='message' AND ts >= ?
                          ORDER BY ts""", (since[:10],)).fetchall()
    for a in apps:
        names = {squash(a["company"])}
        if a["opportunity_id"]:
            o = con.execute("SELECT org FROM opportunities WHERE id=?", (a["opportunity_id"],)).fetchone()
            if o and o["org"]:
                names.add(squash(o["org"]))
        names = {n for n in names if len(n) >= 3}
        if not names:
            continue
        for m in mail:
            if (m["ts"] or "") < a["created_at"][:10]:
                continue
            d = json.loads(m["data"] or "{}")
            if d.get("from_me"):
                continue
            head = squash((d.get("from") or "") + " " + (m["title"] or "") + " " + (m["body"] or "")[:1500])
            if not any(n in head for n in names):
                continue
            text = (m["title"] or "") + "\n" + (m["body"] or "")[:4000]
            if REJECTED.search(text) and a["status"] != "rejected":
                ap.set_state(con, a["id"], status="rejected", note=f"rejection mail, fact {m['id']}")
                db.put_fact(con, "applications", "status", external_id=f"{a['id']}:rejected", ts=m["ts"],
                            title=f"{a['company']}: rejected", body=m["title"],
                            data={"application": a["id"], "mail": m["id"]})
                counts["rejected"] += 1
                break
            if SUBMITTED.search(text) and a["status"] in ("filling", "filled"):
                ap.set_state(con, a["id"], status="submitted", note=f"confirmation mail, fact {m['id']}")
                db.put_fact(con, "applications", "status", external_id=f"{a['id']}:submitted", ts=m["ts"],
                            title=f"{a['company']}: submitted", body=m["title"],
                            data={"application": a["id"], "mail": m["id"]})
                counts["submitted"] += 1
                a = dict(a, status="submitted")
    return counts


if __name__ == "__main__":
    collector.main(NAME, collect)
