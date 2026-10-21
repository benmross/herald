"""The upstream collector offers a release without locking itself out.

It writes the release fact through the collector's connection and the approval
through approvals' own. Both go to one real SQLite file here, because the bug
this guards against (October 2026: every run failed with "database is locked"
once a release existed) only exists between two connections to the same file.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import approvals, config, db  # noqa: E402


def load_collector():
    spec = importlib.util.spec_from_file_location(
        "upstream_collector", ROOT / "collectors" / "upstream.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OfferRelease(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(
            config, "DB_PATH", pathlib.Path(tmp.name) / "facts.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mod = load_collector()
        release = {"tag": "v9.9.9", "from": "v9.9.8", "notes": "notes",
                   "commits": ["one"]}
        for target, attr, value in (
                (self.mod.upstream, "mode", lambda: "tracking"),
                (self.mod.upstream, "fetch", lambda: (True, "")),
                (self.mod.upstream, "available", lambda: release)):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.ask = mock.patch.object(self.mod.notify, "ask").start()
        self.addCleanup(mock.patch.stopall)

    def test_a_new_release_is_offered_once(self):
        con = db.connect()
        con.execute("PRAGMA busy_timeout = 200")
        try:
            self.assertEqual(self.mod.collect(con), {"offered": "v9.9.9"})
            con.commit()
            self.assertEqual(self.mod.collect(con),
                             {"awaiting your answer": "v9.9.9"})
            con.commit()
        finally:
            con.close()
        rows = [r for r in approvals.pending() if r["kind"] == self.mod.KIND]
        self.assertEqual(len(rows), 1)
        self.ask.assert_called_once()


if __name__ == "__main__":
    unittest.main()
