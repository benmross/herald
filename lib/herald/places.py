"""Places from OpenStreetMap, held locally, so "what is open near here" is a query.

Built 23 October 2026, when a question about where to eat after a game near an
arena turned into one web search per restaurant, each answered by a summary of
somebody's listicle. OpenStreetMap already holds, for every mapped restaurant,
the primary facts a search like that needs: its name, its coordinates, its
cuisine, its `opening_hours` in a machine-readable syntax, its website and
phone, and sometimes `diet:*` tags. Downloading a regional extract once makes
every later search local, instant and free, and the website column is what a
second pass (`herald browse crawl`) reads menus and allergen statements from.

HOW IT IS HELD. `places.regions` in config lists Geofabrik extract paths
(`north-america/us/district-of-columbia`). `herald places update` downloads
each `.osm.pbf` into `ledger/raw/places/` (disposable, gitignored, like every
other cache under raw/) and rebuilds `ledger/raw/places/places.db` from them:
one row per named node or way carrying an amenity, shop, tourism, leisure or
cuisine tag. A way's point is the mean of its nodes, which is close enough for
"how far is it" and avoids building polygons. The db is derived, so it is
rebuilt whole every time rather than patched.

WHAT OSM CANNOT TELL YOU. Hours are volunteer-maintained and can be stale, and
an absent `opening_hours` says nothing. `is_open` therefore answers True,
False or None, and None is "OSM does not say" rather than closed. Treat
OSM's answer as a filter that decides which places to check at their own
sites, never as the final word. On the day this was built, OSM listed a vegan
counter beside the arena as open until midnight; it had closed three months
earlier. A chain's own location page usually carries schema.org
`openingHours` in its JSON-LD, which is the primary source to confirm against.

The `opening_hours` parser handles the forms nearly every restaurant uses
(`Mo-Fr 11:00-22:00; Sa,Su 10:00-23:00`, `24/7`, times past midnight, `off`,
`PH off`). Anything with months, dates, weeks or sunrise returns None.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import sqlite3
import urllib.request

from herald import config

DIR = config.RAW / "places"
DB_PATH = DIR / "places.db"
GEOFABRIK = "https://download.geofabrik.de/{region}-latest.osm.pbf"

# The keys that make a named feature a "place" worth holding.
KEYS = ("amenity", "shop", "tourism", "leisure", "cuisine", "craft", "office", "healthcare")
FOOD = ("restaurant", "fast_food", "cafe", "food_court", "pub", "bar", "biergarten", "ice_cream")


def regions() -> list[str]:
    return list(config.get("places.regions") or [])


def download(region: str):
    DIR.mkdir(parents=True, exist_ok=True)
    out = DIR / (region.replace("/", "_") + ".osm.pbf")
    tmp = out.with_suffix(".part")
    req = urllib.request.Request(GEOFABRIK.format(region=region),
                                 headers={"User-Agent": "herald/1.0 (personal places index)"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    tmp.replace(out)
    return out


def build(pbfs: list) -> int:
    """Rebuild places.db from the given extracts. Returns the row count."""
    import osmium  # optional dependency: requirements-optional.txt

    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DB_PATH.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.execute("""CREATE TABLE places (
        osm TEXT PRIMARY KEY, name TEXT, kind TEXT, lat REAL, lon REAL,
        cuisine TEXT, opening_hours TEXT, website TEXT, phone TEXT,
        address TEXT, tags TEXT)""")
    rows = 0
    for pbf in pbfs:
        # The filter runs in C++ after node locations are recorded, so the
        # millions of untagged nodes and ways never reach Python. Without it
        # Maryland alone took over eight minutes.
        fp = (osmium.FileProcessor(str(pbf)).with_locations()
              .with_filter(osmium.filter.KeyFilter(*KEYS)))
        for o in fp:
            if o.is_relation():
                continue
            tags = {t.k: t.v for t in o.tags}
            if "name" not in tags or not any(k in tags for k in KEYS):
                continue
            if o.is_node():
                if not o.location.valid():
                    continue
                lat, lon = o.location.lat, o.location.lon
            else:
                pts = [(n.lat, n.lon) for n in o.nodes if n.location.valid()]
                if not pts:
                    continue
                lat = sum(p[0] for p in pts) / len(pts)
                lon = sum(p[1] for p in pts) / len(pts)
            kind = next((f"{k}={tags[k]}" for k in KEYS if k in tags and k != "cuisine"), "cuisine")
            addr = " ".join(tags[k] for k in ("addr:housenumber", "addr:street") if k in tags)
            con.execute("INSERT OR REPLACE INTO places VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                ("n" if o.is_node() else "w") + str(o.id), tags["name"], kind, lat, lon,
                tags.get("cuisine"), tags.get("opening_hours"),
                tags.get("website") or tags.get("contact:website"),
                tags.get("phone") or tags.get("contact:phone"),
                addr or None, json.dumps(tags, ensure_ascii=False)))
            rows += 1
    con.execute("CREATE INDEX places_name ON places(name)")
    con.commit()
    con.close()
    tmp.replace(DB_PATH)
    return rows


def connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError("no places index: set places.regions and run `herald places update`")
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def distance_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def resolve(where: str) -> tuple[float, float, str]:
    """"lat,lon", a place in the index, or an address (through herald.geo)."""
    if m := re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", where):
        return float(m[1]), float(m[2]), where
    con = connect()
    # Exact name first, then the shortest name containing it: "Stamp Student
    # Union" should find the building, not a statue whose name mentions it.
    row = con.execute("SELECT name, lat, lon FROM places WHERE name = ? COLLATE NOCASE",
                      (where,)).fetchone() or con.execute(
        "SELECT name, lat, lon FROM places WHERE name LIKE ? ORDER BY length(name) LIMIT 1",
        (f"%{where}%",)).fetchone()
    if row:
        return row["lat"], row["lon"], row["name"]
    from herald import geo
    g = geo.geocode(where)
    if not g:
        raise LookupError(f"cannot place {where!r}")
    return g["lat"], g["lon"], g.get("display_name") or where


def near(lat: float, lon: float, *, radius_m: float = 1000, kinds=FOOD,
         cuisine: str | None = None, open_at: dt.datetime | None = None,
         open_for_min: int = 0, include_unknown: bool = True) -> list[dict]:
    dlat = radius_m / 111320
    dlon = radius_m / (111320 * max(0.1, math.cos(math.radians(lat))))
    con = connect()
    out = []
    for r in con.execute("SELECT * FROM places WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                         (lat - dlat, lat + dlat, lon - dlon, lon + dlon)):
        d = distance_m(lat, lon, r["lat"], r["lon"])
        if d > radius_m:
            continue
        kind = r["kind"].split("=", 1)[-1]
        if kinds and kind not in kinds:
            continue
        if cuisine and cuisine.lower() not in (r["cuisine"] or "").lower():
            continue
        row = dict(r)
        row["distance_m"] = round(d)
        row["tags"] = json.loads(r["tags"])
        if open_at is not None:
            st = is_open(r["opening_hours"], open_at)
            if st and open_for_min:
                st = is_open(r["opening_hours"], open_at + dt.timedelta(minutes=open_for_min))
            row["open"] = st
            if st is False or (st is None and not include_unknown):
                continue
        out.append(row)
    out.sort(key=lambda x: x["distance_m"])
    return out


# --- opening_hours ---------------------------------------------------------

DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
_UNSUPPORTED = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|week|sunrise|sunset|dawn|dusk|easter)\b|\[|\d{4}")


def _days(spec: str) -> set[int] | None:
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if part in ("PH", "SH"):
            continue
        m = re.fullmatch(r"(Mo|Tu|We|Th|Fr|Sa|Su)(?:-(Mo|Tu|We|Th|Fr|Sa|Su))?", part)
        if not m:
            return None
        a = DAYS.index(m[1])
        b = DAYS.index(m[2]) if m[2] else a
        i = a
        while True:
            out.add(i)
            if i == b:
                break
            i = (i + 1) % 7
    return out


def _mins(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _intervals(spec: str) -> list[tuple[int, int]] | None:
    """'11:00-14:00,17:00-02:00' -> [(660, 840), (1020, 1560)]; ends past midnight exceed 1440."""
    out = []
    for part in spec.split(","):
        part = part.strip().rstrip("+")
        m = re.fullmatch(r"(\d{1,2}:\d{2})-(\d{1,2}:\d{2})", part)
        if not m:
            return None
        a, b = _mins(m[1]), _mins(m[2])
        if b <= a:
            b += 1440
        out.append((a, b))
    return out


def schedule(oh: str) -> dict[int, list[tuple[int, int]]] | None:
    """Weekday -> intervals in minutes from that day's midnight, or None if unparsed."""
    if not oh or _UNSUPPORTED.search(oh):
        return None
    oh = oh.strip()
    if oh == "24/7":
        return {d: [(0, 1440)] for d in range(7)}
    week: dict[int, list[tuple[int, int]]] = {}
    # "," before a weekday starts another rule too ("Mo-We 10:00-21:00, Th-Sa 10:00-23:00").
    for rule in re.split(r";|(?<=\d),\s*(?=(?:Mo|Tu|We|Th|Fr|Sa|Su|PH)\b)", oh):
        rule = rule.strip().strip('"')
        if not rule:
            continue
        m = re.fullmatch(r"((?:(?:Mo|Tu|We|Th|Fr|Sa|Su|PH|SH)(?:-(?:Mo|Tu|We|Th|Fr|Sa|Su))?,?\s*)+)?\s*(.*)", rule)
        dayspec, times = (m[1] or "").strip().rstrip(","), m[2].strip()
        if dayspec in ("PH", "SH"):
            continue
        days = _days(dayspec) if dayspec else set(range(7))
        if days is None:
            return None
        times = re.sub(r'\s*"[^"]*"\s*$', "", times)
        if times in ("off", "closed"):
            iv: list[tuple[int, int]] = []
        elif times == "24/7" or times == "00:00-24:00":
            iv = [(0, 1440)]
        else:
            iv = _intervals(times)
            if iv is None:
                return None
        for d in days:
            week[d] = iv          # a later rule overrides an earlier one for its days
    return week


def is_open(oh: str | None, when: dt.datetime) -> bool | None:
    week = schedule(oh or "")
    if week is None:
        return None
    t = when.hour * 60 + when.minute
    d = when.weekday()
    if any(a <= t < b for a, b in week.get(d, [])):
        return True
    if any(a <= t + 1440 < b for a, b in week.get((d - 1) % 7, [])):
        return True
    return False


def closes(oh: str | None, when: dt.datetime) -> str | None:
    """When the interval containing `when` ends, as 'H:MM AM', or None."""
    week = schedule(oh or "")
    if week is None:
        return None
    t = when.hour * 60 + when.minute
    d = when.weekday()
    for off, day in ((0, d), (1440, (d - 1) % 7)):
        for a, b in week.get(day, []):
            if a <= t + off < b:
                end = (b - off) % 1440
                return config.clock(dt.time(end // 60, end % 60))
    return None
