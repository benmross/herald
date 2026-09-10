#!/usr/bin/env python
"""Contacts — names Herald can attach to addresses.

The point is not the address book itself, which most people barely maintain. It is that
a bare address in an inbox summary is noise, and the name behind it is a
person. Joined against gmail facts, this is what lets the agent talk about who
wrote rather than what address did.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import collector, db, google  # noqa: E402

NAME = "contacts"
REQUIRES = ('google',)

# How often this is worth running:
# an address book the user barely maintains
CADENCE_MINUTES = 1440
FIELDS = "names,emailAddresses,phoneNumbers,organizations,biographies,metadata"


def collect(con) -> dict:
    svc = google.service("people", "v1")
    counts = {"contacts": 0, "with email": 0}

    db.clear(con, NAME, "person")
    page = None
    while True:
        resp = svc.people().connections().list(
            resourceName="people/me", pageSize=1000,
            personFields=FIELDS, pageToken=page).execute()

        for p in resp.get("connections", []):
            names = p.get("names") or [{}]
            emails = [e.get("value") for e in p.get("emailAddresses") or [] if e.get("value")]
            orgs = p.get("organizations") or []
            display = names[0].get("displayName")
            if not display and not emails:
                continue
            db.put_fact(
                con, NAME, "person", external_id=p["resourceName"],
                title=display or emails[0],
                body=(p.get("biographies") or [{}])[0].get("value"),
                data={
                    "emails": emails,
                    "phones": [n.get("value") for n in p.get("phoneNumbers") or []],
                    "organization": (orgs[0].get("name") if orgs else None),
                    "title": (orgs[0].get("title") if orgs else None),
                },
            )
            counts["contacts"] += 1
            counts["with email"] += int(bool(emails))

        page = resp.get("nextPageToken")
        if not page:
            break

    return counts


if __name__ == "__main__":
    collector.main(NAME, collect)
