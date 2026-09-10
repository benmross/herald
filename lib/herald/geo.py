"""Coordinates and addresses, converted into each other, cheaply and once.

Built 7 September 2026 because two unrelated things both turned out to need
the same capability in the same week: naming where a Dawarich GPS point is
("38.9927, -76.9485" -> "the university campus"), and turning an address a contact
texted the user into coordinates so it can be checked against where the user actually
was. the user asked for this to be a real framework rather than one-off code,
because it is going to get reused -- read this before writing another
haversine loop or another `requests.get(nominatim...)` somewhere else.

THREE LAYERS, CHEAPEST FIRST. Every function below tries them in order and
stops at the first one that answers.

1. **The gazetteer** (`config/secrets.json` -> `"places"`). A short,
   hand-maintained list of named places the user actually goes -- home, their dorm,
   campus -- as `{name, lat, lon, radius_m, confidence, note}`. Free, instant,
   no network, and exact for anywhere the user spends real time. This already
   existed (`dawarich.py` used it directly); it now lives here so every
   caller gets it, not just Dawarich. Nothing is looked up over the network
   for a point inside one of these radii.

2. **The cache** (`ledger/raw/geocode-cache.db`, disposable, gitignored --
   same rule as every other cache under `raw/`). Every address string or
   rounded coordinate pair this module has ever resolved, forever. A person's
   home address does not move; there is no reason to ask twice. This is what
   makes bulk work (mining hundreds of candidate addresses out of a text
   archive) cheap on a second run.

3. **The network** -- OpenStreetMap's public Nominatim API. Only reached for
   something neither of the above already answered.

WHY NOMINATIM'S PUBLIC ENDPOINT AND NOT A SELF-HOSTED GEOCODER, given this box
already runs Dawarich, Postgres and half a dozen other containers: volume.
Nominatim's usage policy caps free use at 1 request/second and asks for a
real User-Agent, which is exactly what a personal ledger doing occasional
lookups needs -- self-hosting Nominatim or Photon means importing a
multi-gigabyte OSM planet/region extract and keeping it updated, to save
requests that, cached, only ever happen once per address. **If volume ever
grows past what politely sharing the public instance supports** -- mining
becomes a standing collector instead of an occasional tool, say -- that
importable Docker image is the documented upgrade path; nothing about this
module's interface would need to change, since `geocode`/`reverse` would just
point `NOMINATIM_URL` at localhost.

RATE LIMITING is automatic and process-global via `_throttle()`: every
network call waits out the 1.1s minimum spacing itself. Bulk callers do not
need to add their own `time.sleep` -- just call `geocode()` in a loop.

USAGE:

    from herald import geo

    geo.geocode("221B Baker Street, London")
    # -> {"lat": 39.128..., "lon": -77.158..., "display_name": "...", "source": "cache"}

    geo.reverse(38.9882, -76.9453)
    # -> {"place": "Denton Hall", "source": "gazetteer"}   (inside a known radius)
    # -> {"place": "...", "lat":, "lon":, "source": "nominatim"}   (otherwise)

    geo.distance_m(lat1, lon1, lat2, lon2)   # haversine, metres
    geo.nearest_known_place(lat, lon)        # -> (name, distance_m) | (None, None)

Every result carries `"source"` so a caller (and anyone reading a printout)
can tell a free gazetteer hit from a cached lookup from a fresh network call.
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

NOMINATIM_URL = "https://nominatim.openstreetmap.org"
# Nominatim's usage policy: max 1 req/s, and identify yourself. This is not
# decoration -- getting IP-banned from the shared public instance is a real
# failure mode and it does not tell you why lookups started returning nothing.
# Nominatim's usage policy asks for a contact address in the User-Agent so an
# abusive client can be reached rather than blocked. It is the user's own, from
# their config, and it goes to OpenStreetMap and nowhere else -- which is worth
# knowing before enabling anything that geocodes.
def _user_agent() -> str:
    contact = config.get("user.email") or "no contact configured"
    return f"herald/1.0 (personal ledger for a single user; contact {contact})"


USER_AGENT = _user_agent()
MIN_INTERVAL_S = 1.1

CACHE_PATH = config.RAW / "geocode-cache.db"

_last_request = 0.0


def _cache() -> sqlite3.Connection:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(CACHE_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS geocode (
            kind      TEXT NOT NULL,   -- 'forward' or 'reverse'
            key       TEXT NOT NULL,   -- normalized address, or "lat,lon" rounded to 4dp
            response  TEXT,            -- JSON, the parsed result (or "null" for a miss)
            ts        TEXT NOT NULL,
            PRIMARY KEY (kind, key)
        )
    """)
    return con


def _throttle() -> None:
    global _last_request
    wait = MIN_INTERVAL_S - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def _get(path: str, params: dict) -> list | dict | None:
    _throttle()
    url = f"{NOMINATIM_URL}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as e:
        print(f"geo: nominatim request failed: {e}", file=sys.stderr)
        return None


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres. The one haversine, used everywhere."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def known_places() -> list[dict]:
    """The hand-maintained gazetteer from config/secrets.json -> 'places'."""
    return config.secret("places", []) or []


def nearest_known_place(lat: float, lon: float) -> tuple[str | None, float | None]:
    """The nearest gazetteer place within its own radius, and the distance to it.

    (None, None) if lat/lon is missing or nothing in the gazetteer is close
    enough. This never touches the network or the cache -- it is the free
    first layer every other function in this module tries first.
    """
    if lat is None or lon is None:
        return None, None
    best, best_d = None, None
    for p in known_places():
        try:
            d = distance_m(float(lat), float(lon), float(p["lat"]), float(p["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
        if d <= p.get("radius_m", 150) and (best_d is None or d < best_d):
            best, best_d = p.get("name"), d
    return best, (round(best_d) if best_d is not None else None)


# A soft geographic bias, not a filter: an address with no city/state -- "22011
# Dickerson rd" is a real example that came out of a text -- is genuinely
# ambiguous across the whole US, and Nominatim's default ranking has no idea
# that almost everyone in this ledger lives within an hour of Washington, DC.
# Found the hard way: an unqualified "Dickerson Rd" resolved to Wisner
# Township, Michigan instead of Dickerson, Maryland, and got written to a real
# contact before the mistake was caught. `viewbox` without `bounded=1` is a
# *preference*, not a restriction -- a genuinely distant address (a friend at
# college out of state, say) still resolves, just without the free home-turf
# advantage. Overridable per call for anyone who really is asking about
# somewhere else entirely.
LOCAL_BIAS_VIEWBOX = "-77.6,39.4,-76.6,38.7"   # lon/lat min,max around DC-MD-VA


def geocode(address: str, *, bias_local: bool = True) -> dict | None:
    """Address string -> {"lat", "lon", "display_name", "source"}, or None.

    "source" is "cache" or "nominatim" so a caller can tell whether this cost
    a network round trip. Never raises -- a bad address or a network hiccup
    is a None, not an exception, because this is meant to run unattended over
    a batch of candidates where one bad entry should not stop the rest.
    """
    key = " ".join(address.split()).lower()
    if not key:
        return None

    con = _cache()
    row = con.execute("SELECT response FROM geocode WHERE kind='forward' AND key=?",
                       (key,)).fetchone()
    if row:
        parsed = json.loads(row[0])
        if parsed:
            parsed["source"] = "cache"
        return parsed

    params = {"q": address, "format": "jsonv2", "limit": 1, "countrycodes": "us"}
    if bias_local:
        params["viewbox"] = LOCAL_BIAS_VIEWBOX
    results = _get("/search", params)
    result = None
    if results:
        best = results[0]
        result = {"lat": float(best["lat"]), "lon": float(best["lon"]),
                  "display_name": best.get("display_name")}

    con.execute("INSERT OR REPLACE INTO geocode VALUES ('forward', ?, ?, ?)",
                (key, json.dumps(result), time.strftime("%Y-%m-%dT%H:%M:%S")))
    con.commit()
    con.close()
    if result:
        result["source"] = "nominatim"
    return result


def reverse(lat: float, lon: float) -> dict | None:
    """Coordinates -> {"place", "source", ...}, checking the gazetteer first.

    A gazetteer hit returns just {"place": name, "source": "gazetteer",
    "metres_from_place": ...} -- that is genuinely all the user would want to be
    told ("you're at Denton Hall"), and it costs nothing. Only a coordinate
    outside every known radius reaches the cache or the network, and gets the
    fuller {"place": display_name, "lat", "lon", "source"} shape.
    """
    name, distance = nearest_known_place(lat, lon)
    if name:
        return {"place": name, "metres_from_place": distance, "source": "gazetteer"}

    key = f"{round(lat, 4)},{round(lon, 4)}"
    con = _cache()
    row = con.execute("SELECT response FROM geocode WHERE kind='reverse' AND key=?",
                       (key,)).fetchone()
    if row:
        parsed = json.loads(row[0])
        if parsed:
            parsed["source"] = "cache"
        return parsed

    result_json = _get("/reverse", {"lat": lat, "lon": lon, "format": "jsonv2"})
    result = None
    if result_json and result_json.get("display_name"):
        result = {"place": result_json["display_name"], "lat": lat, "lon": lon}

    con.execute("INSERT OR REPLACE INTO geocode VALUES ('reverse', ?, ?, ?)",
                (key, json.dumps(result), time.strftime("%Y-%m-%dT%H:%M:%S")))
    con.commit()
    con.close()
    if result:
        result["source"] = "nominatim"
    return result
