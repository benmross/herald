"""herald browse: the parts that keep a session from doing something red
(22 Oct 2026).

What must hold: a click whose label reads like a final submit is refused, while
cookie banners and filter buttons are not; a user-view link is unguessable,
expires, and stops working once closed; a crawl list parses either form.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import browse  # noqa: E402


class Refusal(unittest.TestCase):
    def test_submits_refused(self):
        for label in ("Submit application", "Apply now", "Send", "Place order",
                      "Confirm and pay", "Register", "Post"):
            with self.assertRaises(SystemExit, msg=label):
                browse.refuse_submit(label, filled=8)

    def test_apply_opens_but_does_not_submit(self):
        browse.refuse_submit("Apply Manually", filled=0)
        browse.refuse_submit("Apply", filled=1)
        with self.assertRaises(SystemExit):
            browse.refuse_submit("Apply", filled=8)
        with self.assertRaises(SystemExit):
            browse.refuse_submit("Submit application", filled=0)

    def test_harmless_allowed(self):
        for label in ("Accept all cookies", "Accept", "Apply filters", "Sign in",
                      "Next", "Search", "Load more"):
            browse.refuse_submit(label)


class Views(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.p = mock.patch.object(browse, "spool", lambda: self.tmp)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_token_round_trip_and_close(self):
        t = browse.new_view("abc123", "sign in to a site", 10)
        self.assertGreaterEqual(len(t), 32)
        self.assertEqual(browse.view(t)["tab"], "abc123")
        # The spool holds a hash, never the token.
        self.assertFalse(any(t in f.read_text() or t in f.name for f in self.tmp.iterdir()))
        browse.close_view(t)
        self.assertIsNone(browse.view(t))

    def test_expiry_and_junk(self):
        t = browse.new_view("abc123", "x", 10)
        with mock.patch.object(time, "time", return_value=time.time() + 3600):
            self.assertIsNone(browse.view(t))
        self.assertIsNone(browse.view("../../etc/passwd"))
        self.assertIsNone(browse.view("A" * 32))


class Jobs(unittest.TestCase):
    def test_both_forms(self):
        f = pathlib.Path(tempfile.mkdtemp()) / "u.txt"
        f.write_text("# comment\nhttps://a.example/x\nid 7\thttps://b.example/y\n\n")
        jobs = browse.read_jobs(str(f))
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[1], ("id_7", "https://b.example/y"))
        self.assertEqual(len(jobs[0][0]), 12)


if __name__ == "__main__":
    unittest.main()
