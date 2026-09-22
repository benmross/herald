"""The cycles' door has to refuse everything outside amber, without a network.

On 22 September 2026 the dawn cycle read that a lecture had moved back to its
usual room and changed nothing, because its prompt forbade any write outside
the ledger and its tools could not make one. Giving cycles a door fixes that,
and makes these refusals load-bearing: a scheduled run with nobody watching now
holds a tool that writes to the calendar. Each test below is a way that could
reach another person or a calendar that is not the user's.
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "cycles"))

from herald import amber  # noqa: E402

ROLES = {"Mine": "mine", "Feed": "feed", "Ref": "reference", "Mum": "other-person"}


def _get(path, default=None):
    return ROLES if path == "calendars.roles" else default


class Calendars(unittest.TestCase):
    def test_only_mine(self):
        with mock.patch.object(amber.config, "get", _get), \
                mock.patch.object(amber.gwrite, "calendar_id", lambda n: "cal-" + n):
            self.assertEqual(amber.calendar("Mine"), "cal-Mine")
            for name in ("Feed", "Ref", "Mum", "Unlisted"):
                with self.assertRaises(amber.Refused, msg=name):
                    amber.calendar(name)


class Privacy(unittest.TestCase):
    def test_other_attendee_refused(self):
        ev = {"summary": "coffee", "attendees": [{"email": "a", "self": True},
                                                 {"email": "b"}]}
        with self.assertRaises(amber.Refused):
            amber._private(ev, "cal")

    def test_someone_elses_invite_refused(self):
        ev = {"summary": "their meeting", "organizer": {"email": "them@x"}}
        with self.assertRaises(amber.Refused):
            amber._private(ev, "cal")

    def test_own_and_group_calendar_events_allowed(self):
        amber._private({"summary": "a", "organizer": {"self": True}}, "cal")
        amber._private({"summary": "b", "organizer": {"email": "cal"}}, "cal")
        amber._private({"summary": "c"}, "cal")


class Why(unittest.TestCase):
    def test_a_reason_is_required(self):
        for bad in ("", "  ", "because"):
            with self.assertRaises(amber.Refused):
                amber._why(bad)
        self.assertTrue(amber._why("instructor announcement, fact #1"))

    def test_event_id_accepts_fact_external_id(self):
        self.assertEqual(amber._event_id("cal@group::abc_2026"), "abc_2026")
        self.assertEqual(amber._event_id("abc_2026"), "abc_2026")


class CycleAllowlists(unittest.TestCase):
    """A cycle may reach amber and must never reach a red kind."""

    def test_cycles_get_amber_not_act(self):
        for cycle in ("dawn", "scout"):
            src = (ROOT / "cycles" / f"{cycle}.py").read_text()
            self.assertIn("amber.CYCLE_TOOLS", src, cycle)
            self.assertNotIn("herald act", src, cycle)
        self.assertEqual(amber.CYCLE_TOOLS, ["Bash(herald amber *)"])

    def test_rules_render(self):
        with mock.patch.object(amber.config, "person",
                               lambda: {"subject": "they", "object": "them",
                                        "possessive": "their"}):
            text = amber.cycle_rules()
        self.assertIn("herald amber update", text)
        self.assertNotIn("{", text)


if __name__ == "__main__":
    unittest.main()
