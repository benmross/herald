"""The Slack collector: message storage and cursor handling, with no network."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))


def _load():
    spec = importlib.util.spec_from_file_location("slack_collector", ROOT / "collectors/slack.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SlackTest(unittest.TestCase):
    def test_labels(self):
        m = _load()
        users = {"U1": "Ada"}
        self.assertEqual(m._label({"id": "C1", "name": "general"}, users), "#general")
        self.assertEqual(m._label({"id": "D1", "is_im": True, "user": "U1"}, users), "DM with Ada")

    def test_iso(self):
        self.assertEqual(_load()._iso("0"), "1970-01-01T00:00:00+00:00")

    def test_declares_cadence_and_requirement(self):
        m = _load()
        self.assertEqual(m.REQUIRES, ("slack",))
        self.assertGreater(m.CADENCE_MINUTES, 0)


if __name__ == "__main__":
    unittest.main()
