"""Where the user is, from a location they share on Telegram.

Herald's sense of where someone is came only from a location server (the
dawarich extension), which almost nobody else runs. Telegram already lets a
person send their position, or share it live for up to eight hours, and the
bridge used to drop those messages without a word because they carry no text.

Each one becomes the `telegram/current` fact. Anything that asks where the
user is reads the newest `current` fact from any source (the brief, the
orientation, a transit plan from "here"), so this needs no consumer of its
own. A live share arrives as a stream of edited messages; each overwrites the
same row, so the fact is always the latest fix and never a history. Location
history stays the job of a source built for it.
"""

from __future__ import annotations

import datetime as dt

from . import config, db, geo

SOURCE = "telegram"


def record(lat: float, lon: float, *, accuracy_m: float | None = None,
           live: bool = False, when: dt.datetime | None = None) -> str:
    """Store one fix and return how to name it to the user."""
    when = when or dt.datetime.now(config.tz())
    name, distance = geo.nearest_known_place(lat, lon)
    title = name or f"{lat:.4f}, {lon:.4f}"
    with db.session() as con:
        db.put_fact(
            con, SOURCE, "current", external_id="latest",
            ts=when.isoformat(timespec="seconds"), title=title,
            data={"lat": lat, "lon": lon, "place": name,
                  "metres_from_place": distance, "named": bool(name),
                  "accuracy_m": accuracy_m, "live": live, "age_minutes": 0,
                  "via": "telegram"})
    return title
