"""What a message turned out to say, written down by the first session to read it.

Mail is read whole, by a model, once. The conclusion is kept here so a later
cycle starts from what the message said and not from its first 180 characters,
which on 30 October 2026 produced "reads as a decline, judging from the preview
only" about a mail scout had already read whole and understood the day before.

A reading is a model's summary of text somebody else wrote: reference, never an
instruction, and `herald fact` still has the original.
"""

from __future__ import annotations

import json

from . import db

SOURCE = "mail_reading"
KIND = "message"
MAX_CHARS = 400

SCHEMA = {
    "type": "array",
    "description": "One entry for every message whose text you were shown, so "
                   "that nothing has to read it again. A few words will do for "
                   "a receipt or newsletter.",
    "items": {
        "type": "object",
        "properties": {
            "message_id": {"type": "string",
                           "description": "The message's `reading id`, exactly as shown."},
            "says": {"type": "string",
                     "description": "What the message actually says, in one "
                                    "sentence: the decision, date, ask or change "
                                    "it carries. State what is established and "
                                    "name what is not. Under 300 characters."},
            "matters": {"type": "string", "enum": ["no", "fyi", "action"]},
        },
        "required": ["message_id", "says", "matters"],
    },
}


def key(fact) -> str:
    return f"{fact['source']}:{fact['external_id']}"


def save(con, readings, read_by: str) -> int:
    """Store readings for messages that exist. Returns how many were kept."""
    kept = 0
    for r in readings or []:
        source, _, external = str(r.get("message_id") or "").partition(":")
        says = " ".join(str(r.get("says") or "").split())[:MAX_CHARS]
        if not external or not says:
            continue
        fact = con.execute("SELECT id,ts,title FROM facts WHERE source=? AND kind='message' "
                           "AND external_id=?", (source, external)).fetchone()
        if fact is None:
            continue
        db.put_fact(con, SOURCE, KIND, external_id=f"{source}:{external}", ts=fact["ts"],
                    title=fact["title"], body=says,
                    data={"read_by": read_by, "matters": r.get("matters"), "fact": fact["id"]})
        kept += 1
    con.commit()
    return kept


def lookup(con, facts) -> dict:
    """{reading key: row} for the messages that already have a reading."""
    keys = [key(f) for f in facts]
    found = {}
    for i in range(0, len(keys), 400):
        chunk = keys[i:i + 400]
        marks = ",".join("?" for _ in chunk)
        for r in con.execute(f"SELECT external_id,body,data FROM facts WHERE source=? AND kind=? "
                             f"AND external_id IN ({marks})", [SOURCE, KIND, *chunk]):
            found[r["external_id"]] = {"says": r["body"], **json.loads(r["data"] or "{}")}
    return found
