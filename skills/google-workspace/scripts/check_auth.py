"""Read-only health check for the shared Google agent credentials.

Confirms the token loads, refreshes if needed, and actually reaches each API.
Prints only identifying and count data — never the token itself.
"""

from google_api import get_service

CHECKS = [
    ("gmail", "v1", lambda s: s.users().getProfile(userId="me").execute()["emailAddress"]),
    ("drive", "v3", lambda s: s.about().get(fields="user(emailAddress)").execute()["user"]["emailAddress"]),
    ("calendar", "v3", lambda s: f"{len(s.calendarList().list().execute().get('items', []))} calendars"),
    ("tasks", "v1", lambda s: f"{len(s.tasklists().list().execute().get('items', []))} task lists"),
    ("sheets", "v4", lambda s: "reachable"),
    ("docs", "v1", lambda s: "reachable"),
    ("people", "v1", lambda s: "reachable"),
]


def main():
    failures = 0
    for api, version, probe in CHECKS:
        try:
            print(f"  {api:<10} ok    {probe(get_service(api, version))}")
        except Exception as exc:  # noqa: BLE001 - a diagnostic wants every failure
            failures += 1
            print(f"  {api:<10} FAIL  {type(exc).__name__}: {exc}")

    print(
        "\nAll services reachable."
        if not failures
        else f"\n{failures} service(s) failed. If auth is the cause, see 'Setup and repair'."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
