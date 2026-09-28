"""`calendars.ignore` has to outrank `calendars.roles`, at the source.

It used to be honoured by one section of one snapshot while the calendar kept
role "mine", so a live session that filtered on role, and the cycles' amber
door, both read a shared family calendar as the user's own commitments. The
user had to say it three times. A role stamped by the collector is the one
thing every reader already checks.
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "collectors"))

import gcal  # noqa: E402

CONF = {"calendars.roles": {"Family": "mine", "Classes": "mine"},
        "calendars.ignore": ["Family", "someone@example.com"]}


def _get(path, default=None):
    return CONF.get(path, default)


class Roles(unittest.TestCase):
    def test_ignore_outranks_roles(self):
        with mock.patch.object(gcal.config, "get", _get):
            self.assertEqual(gcal._role({"summary": "Family", "id": "x"}), "ignored")
            self.assertEqual(gcal._role({"summary": "Mum", "id": "someone@example.com"}),
                             "ignored")
            self.assertEqual(gcal._role({"summary": "Classes", "id": "y"}), "mine")
            self.assertEqual(gcal._role({"summary": "Other", "id": "z"}), "feed")

    def test_amber_refuses_ignored_even_if_roles_say_mine(self):
        # The live config had Family as "mine" in roles *and* in ignore; the
        # amber door read only roles, so a cycle could have edited an event
        # the user's family sees.
        from herald import amber
        with mock.patch.object(amber.config, "get", _get):
            with self.assertRaises(amber.Refused):
                amber.calendar("Family")


if __name__ == "__main__":
    unittest.main()
