"""Events on a calendar in `calendars.ignore` get role "ignored".

The gcal collector now stamps that role at the source, but it pulls deltas
through sync tokens, so an event that has not changed is never rewritten and
would keep the old role -- often "mine" -- forever. This re-stamps what is
already stored.
"""

from __future__ import annotations

from herald import config

DESCRIPTION = "events and calendars listed in calendars.ignore get role 'ignored'"


def apply(con) -> str:
    ignored = list(config.get("calendars.ignore", []) or [])
    if not ignored:
        return "nothing listed in calendars.ignore"
    marks = ",".join("?" * len(ignored))
    events = con.execute(f"""
        UPDATE facts SET data = json_set(data, '$.role', 'ignored')
        WHERE source='gcal' AND kind='event'
          AND (json_extract(data,'$.calendar') IN ({marks})
               OR json_extract(data,'$.calendar_id') IN ({marks}))
          AND COALESCE(json_extract(data,'$.role'),'') != 'ignored'
    """, (*ignored, *ignored)).rowcount
    cals = con.execute(f"""
        UPDATE facts SET data = json_set(data, '$.role', 'ignored')
        WHERE source='gcal' AND kind='calendar'
          AND (title IN ({marks}) OR external_id IN ({marks}))
          AND COALESCE(json_extract(data,'$.role'),'') != 'ignored'
    """, (*ignored, *ignored)).rowcount
    return f"{events} events and {cals} calendars re-stamped"
