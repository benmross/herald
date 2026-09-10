#!/usr/bin/env python
"""Internship postings — from the lists the internet already maintains.

The question this was built to answer: could a recruiting round somebody heard
about by word of mouth have been found online instead? Checked rather than
assumed, the answer was no -- one large defence contractor had zero postings on
a list carrying sixteen thousand, while several of its competitors were on it.
Coverage of any given employer is uneven, and some recruit only through their
own site.

So this collector is half the answer. It pulls the crowdsourced list, which is
excellent for tech and updated daily. The other half is `watch_employers` in the
config: companies the user cares about that these lists miss, which the scout cycle
checks directly on a slower cadence.

One thing the data does NOT support, so nothing here pretends otherwise: not one
of the 3,082 active listings names a first- or second-year programme in its
title. Freshman eligibility cannot be filtered from this feed. Google STEP is
findable only because it is a known name, and the eligibility line lives on the
job page rather than in the index.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import collector, db  # noqa: E402

NAME = "internships"
REQUIRES = ('jobs',)

# How often this is worth running:
# downloads 24 MB of listings; twice a day matches scout
CADENCE_MINUTES = 720

SOURCES = {
    "simplify": "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships"
                "/dev/.github/scripts/listings.json",
    "vansh": "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships"
             "/dev/.github/scripts/listings.json",
}

CATEGORIES = {"Software", "AI/ML/Data", "Hardware", "Quant", "Product",
              "Software Engineering", "Data Science, AI & Machine Learning",
              "Hardware Engineering"}

# Somewhere close enough to live at home needs no housing problem solved;
# anywhere else does, which is a real filter for a student. The region is a
# config setting -- `jobs.home_region` -- and the pattern below is its default.
# Bare city names are a trap. "Columbia" caught Columbia, South Carolina;
# "Washington" catches Washington State; "Hanover" is in New Hampshire and
# Pennsylvania too. Anchor on the state, or on a city distinctive enough to
# stand alone.
DMV = re.compile(
    r"\b(MD|Maryland|DC|Virginia)\b"
    r"|\bVA\b(?!\s*\w)"
    r"|\bWashington,?\s*D\.?C\.?"
    r"|\b(College Park|Bethesda|Rockville|Silver Spring|Greenbelt|Towson|"
    r"Annapolis|Gaithersburg|Germantown|Fort Meade|Aberdeen|"
    r"Arlington|Alexandria|Reston|McLean|Tysons|Herndon|Chantilly|Quantico)\b",
    re.I)
REMOTE = re.compile(r"remote|anywhere|us-based", re.I)

# The named early-career programmes. None of them are labelled as such in the
# feed, so this is a keyword list rather than a filter on structured data.
EARLY_CAREER = re.compile(
    r"\b(STEP|Explore Microsoft|Below the Line|Career Prep|Accelerate|"
    r"First[- ]Year|Freshman|Sophomore|Rising Sophomore|Early Identification|"
    r"Discovery|Launchpad|Ignite|Emerge|Pathways|Insight|Springboard)\b", re.I)

MAX_AGE_DAYS = 120


def _fetch(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "herald/0.1"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def _watched(company: str, watch: set[str]) -> bool:
    """Is this one of the employers the user is actually watching?

    Substring matching on company names is a trap: "NSA" is inside "Sensata",
    which is how an electrical-engineering role at a sensor manufacturer ended
    up on a list of places the user wants to work. Match whole words, or the full name
    for multi-word entries.
    """
    name = company.lower()
    words = set(re.findall(r"[a-z0-9]+", name))
    for w in watch:
        w = w.lower().strip()
        if " " in w:
            if w in name:                 # multi-word employer names match as a phrase
                return True
        elif w in words:                  # whole word only
            return True
    return False


def _relevant(row: dict, watch: set[str]) -> tuple[bool, list[str]]:
    if not (row.get("active") and row.get("is_visible", True)):
        return False, []
    if row.get("category") and row["category"] not in CATEGORIES:
        return False, []

    posted = row.get("date_posted") or 0
    if posted and (dt.datetime.now().timestamp() - posted) > MAX_AGE_DAYS * 86400:
        return False, []

    why = []
    locations = " | ".join(row.get("locations") or [])
    company = (row.get("company_name") or "").lower()
    title = row.get("title") or ""

    early = bool(EARLY_CAREER.search(title))
    watched = _watched(company, watch)
    dmv = bool(DMV.search(locations))
    remote = bool(REMOTE.search(locations))

    if early:
        why.append("named early-career programme")
    if watched:
        why.append("watched employer")
    if dmv:
        why.append("DMV")
    if remote:
        why.append("remote")

    # corrected once, and rightly.
    #
    # An earlier version gated on "a programme for their year, or a watched
    # employer", because the DMV slice reads as cleared-defence-contractor work
    # and that resembled the summer the user did not enjoy. But their complaint about
    # that summer was idleness and bureaucracy, not the sector: the user is open to
    # every employer, would take DoD work if it were the best option available,
    # and only mildly prefers not to. Turning a preference into an exclusion
    # decided for them, on the strength of one bad summer, which is exactly the
    # kind of inference this system should not be making.
    #
    # So proximity admits again. Ranking is where the preference belongs.
    admitted = bool(why)
    return admitted, why


def collect(con) -> dict:
    from herald import config

    watch = {w.lower() for w in (config.get("opportunities.watch_employers", []) or [])}
    counts = {"fetched": 0, "relevant": 0, "sources": 0}
    seen_ids: set[str] = set()

    snapshot = config.RAW / "internships"
    snapshot.mkdir(parents=True, exist_ok=True)

    for source, url in SOURCES.items():
        try:
            rows = _fetch(url)
        except Exception as e:                            # noqa: BLE001
            print(f"  {source}: {e}", file=sys.stderr)
            continue
        counts["sources"] += 1
        counts["fetched"] += len(rows)
        (snapshot / f"{source}.json").write_text(json.dumps(rows)[:0] or "")

        for row in rows:
            ok, why = _relevant(row, watch)
            if not ok:
                continue
            ext = f"{source}:{row.get('id') or row.get('url')}"
            if ext in seen_ids:
                continue
            seen_ids.add(ext)

            posted = row.get("date_posted")
            ts = (dt.datetime.fromtimestamp(posted).isoformat(timespec="seconds")
                  if posted else None)
            db.put_fact(
                con, NAME, "posting", external_id=ext, ts=ts,
                title=f"{row.get('company_name')}: {row.get('title')}",
                body=", ".join(row.get("locations") or []) or None,
                data={
                    "company": row.get("company_name"),
                    "role": row.get("title"),
                    "url": row.get("url"),
                    "locations": row.get("locations"),
                    "category": row.get("category"),
                    "terms": row.get("terms"),
                    "sponsorship": row.get("sponsorship"),
                    "degrees": row.get("degrees"),
                    "source": source,
                    "why": why,
                },
            )
            counts["relevant"] += 1

    # Anything that dropped out of the feed has closed. Keeping it would mean
    # offering the user a posting that no longer exists.
    stale = con.execute(
        "DELETE FROM facts WHERE source = ? AND kind = 'posting'"
        " AND external_id NOT IN (%s)" % ",".join("?" * len(seen_ids)),
        (NAME, *seen_ids)).rowcount if seen_ids else 0
    counts["closed"] = stale

    return counts


if __name__ == "__main__":
    collector.main(NAME, collect)
