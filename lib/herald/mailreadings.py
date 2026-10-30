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
import hashlib

from . import db

SOURCE = "mail_reading"
KIND = "message"
MAX_CHARS = 400

SCHEMA = {
    "type": "array",
    "description": "Summaries only for versioned reading IDs supplied with complete "
                   "message text. Omit clipped, missing or unavailable text. "
                   "These are text interpretations, not attachment readings.",
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


def digest(fact) -> str:
    data = json.loads(fact['data'] or '{}')
    content = [fact['title'], fact['body'],
               {k: data.get(k) for k in ('from', 'body_format', 'body_truncated', 'attachments')}]
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def reading_id(fact, *, complete: bool = True) -> str | None:
    """Issue a version token only for complete stored text actually presented."""
    data = json.loads(fact['data'] or '{}')
    if (not complete or not (fact['body'] or '').strip() or data.get('body_truncated')
            or data.get('body_format') not in ('text', 'html')):
        return None
    return f'{key(fact)}#{digest(fact)}'


def save(con, readings, read_by: str) -> int:
    """Store only readings bound to the version presented; legacy IDs are unsafe."""
    kept = 0
    for r in readings or []:
        token = str(r.get("message_id") or "")
        identity, separator, version = token.rpartition('#')
        source, _, external = identity.partition(":")
        says = " ".join(str(r.get("says") or "").split())[:MAX_CHARS]
        if not separator or not version or not external or not says:
            continue
        fact = con.execute("SELECT * FROM facts WHERE source=? AND kind='message' "
                           "AND external_id=?", (source, external)).fetchone()
        if fact is None or reading_id(fact) != token:
            continue
        db.put_fact(con, SOURCE, KIND, external_id=f"{source}:{external}", ts=fact["ts"],
                    title=fact["title"], body=says,
                    data={"read_by": read_by, "matters": r.get("matters"), "fact": fact["id"],
                          "content_digest": version, "text_complete": True})
        kept += 1
    con.commit()
    return kept


def lookup(con, facts) -> dict:
    """{reading key: row} for the messages that already have a reading."""
    versions = {key(f): digest(f) for f in facts if reading_id(f)}
    keys = list(versions)
    found = {}
    for i in range(0, len(keys), 400):
        chunk = keys[i:i + 400]
        marks = ",".join("?" for _ in chunk)
        for r in con.execute(f"SELECT external_id,body,data FROM facts WHERE source=? AND kind=? "
                             f"AND external_id IN ({marks})", [SOURCE, KIND, *chunk]):
            data = json.loads(r['data'] or '{}')
            if data.get('text_complete') and data.get('content_digest') == versions[r['external_id']]:
                found[r["external_id"]] = {"says": r["body"], **data}
    return found
