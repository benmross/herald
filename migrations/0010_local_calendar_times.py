"""Store timed calendar events in the user's timezone, as the collector now does."""

import datetime as dt
import json

from herald import config

DESCRIPTION = "rewrite calendar event times stored in another timezone, UTC feeds above all"


def _local(value):
    if not isinstance(value, str) or "T" not in value:
        return value
    moment = dt.datetime.fromisoformat(value)
    return moment.astimezone(config.tz()).isoformat() if moment.tzinfo else value


def apply(con) -> str:
    changed = 0
    rows = con.execute("SELECT id, ts, data FROM facts WHERE source='gcal' AND kind='event'").fetchall()
    for row in rows:
        data = json.loads(row["data"] or "{}")
        ts, end = _local(row["ts"]), _local(data.get("end"))
        if ts == row["ts"] and end == data.get("end"):
            continue
        data["end"] = end
        con.execute("UPDATE facts SET ts=?, data=? WHERE id=?",
                    (ts, json.dumps(data, default=str, sort_keys=True), row["id"]))
        changed += 1
    return f"{changed} of {len(rows)} calendar events moved to the local timezone"
