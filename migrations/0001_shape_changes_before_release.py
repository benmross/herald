"""The three shape changes made by hand on 9-10 September 2026.

Every install created after that starts with the right shape, so this exists
for exactly one database -- the one it was written on -- and for the honesty of
having a numbered starting point rather than a gap. `herald setup` stamps it
without running it.

Each statement is written to no-op when the shape is already current, because
being recorded is the guard and being safe to run twice is the belt.
"""

DESCRIPTION = ("calendar feed facts use data.tag, mail threads use "
               "user_has_replied, retired collector rows are dropped")


def apply(con) -> str:
    changed = []

    # `data.course` became `data.tag` when the Canvas-only collector became a
    # general calendar-feed one and the bracketed suffix stopped meaning a
    # course code.
    n = con.execute("""
        UPDATE facts
           SET data = json_set(json_remove(data, '$.course', '$.course_id'),
                               '$.tag', json_extract(data, '$.course'),
                               '$.context_id', json_extract(data, '$.course_id'))
         WHERE json_extract(data, '$.course') IS NOT NULL
    """).rowcount
    if n:
        changed.append(f"{n} facts renamed course -> tag")

    n = con.execute("UPDATE facts SET kind='tag' "
                    "WHERE kind='course' AND source IN "
                    "(SELECT DISTINCT source FROM facts WHERE kind='assignment')").rowcount
    if n:
        changed.append(f"{n} course facts became tag facts")

    # A field named after one person.
    n = con.execute("""
        UPDATE facts
           SET data = json_set(json_remove(data, '$.ben_has_replied'),
                               '$.user_has_replied',
                               json_extract(data, '$.ben_has_replied'))
         WHERE json_extract(data, '$.ben_has_replied') IS NOT NULL
    """).rowcount
    if n:
        changed.append(f"{n} threads renamed ben_has_replied -> user_has_replied")

    # Collectors that no longer exist leave a state row that reads as a live
    # collector which has stopped running.
    n = con.execute("DELETE FROM collector_state WHERE collector IN "
                    "('canvas', 'health', 'elms')").rowcount
    if n:
        changed.append(f"{n} retired collector_state rows removed")

    return "; ".join(changed) or "nothing to change"
