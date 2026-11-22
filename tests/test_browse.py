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
        # Workspace views exist only where systemd runs the browser workers.
        # Pretend it does, so these pass in a container or on a Mac too.
        from herald import browser_workspaces as bw, browse_tabs
        self.workers = mock.patch.object(bw, "managed_workers", return_value=True)
        self.workers.start()
        self.state = pathlib.Path(tempfile.mkdtemp())       # not in the spool, whose files a test reads
        self.registry = mock.patch.object(browse_tabs, "_path", lambda: self.state / "tabs.db")
        self.registry.start()
        self.locks = mock.patch.object(browse_tabs, "_lock_path", lambda w, t: str(self.state / f"{w}-{t}.lock"))
        self.locks.start()
        self.screen = mock.patch.object(browse, "vnc_enabled", return_value=False)
        self.screen.start()

    def tearDown(self):
        self.screen.stop()
        self.locks.stop()
        self.registry.stop()
        self.workers.stop()
        self.p.stop()

    def test_a_default_browser_link_reserves_one_tab_not_the_browser(self):
        # A sign-in link used to put the whole default browser in the
        # person's hands, which stopped every agent and collector until
        # they tapped Done. It now reserves the tab it is for.
        from herald import browser_workspaces as bw, browse_tabs
        with mock.patch.object(bw, "take_control") as take:
            t = browse.new_view("abc123", "sign in to a site", 10, workspace="default")
            take.assert_not_called()
        self.assertTrue(browse_tabs.with_person(browse_tabs.get("abc123", "default")))
        self.assertFalse(browse_tabs.with_person(browse_tabs.get("fff000", "default")))
        self.assertNotIn("/groups/", browse.view_link(t) or "")
        second = browse.new_view("abc123", "a replacement link", 10, workspace="default")
        browse.close_view(t)
        self.assertTrue(browse_tabs.with_person(browse_tabs.get("abc123", "default")),
                        "another live link still points at the tab")
        browse.close_view(second)
        self.assertFalse(browse_tabs.with_person(browse_tabs.get("abc123", "default")))

    def test_an_expired_link_stops_reserving_its_tab(self):
        from herald import browse_tabs
        browse.new_view("abc123", "sign in", 10, workspace="default")
        with mock.patch.object(time, "time", return_value=time.time() + 3600):
            self.assertFalse(browse_tabs.with_person(browse_tabs.get("abc123", "default")))

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

    def test_return_revokes_all_workspace_links_and_disconnects_live_screen_first(self):
        from herald import browser_workspaces as bw
        with mock.patch.object(bw,'take_control'),mock.patch.object(bw,'ports',return_value={'vnc':5994,'display':94}),mock.patch.object(browse,'vnc_dir',return_value=self.tmp/'vnc'),mock.patch.object(browse,'vnc_enabled',return_value=True),mock.patch.object(browse.subprocess,'run',return_value=mock.Mock(returncode=0)) as disconnect,mock.patch.object(bw,'release_control') as release:
            a=browse.new_view('one','sign in',10,workspace='example')
            b=browse.new_view('two','sign in',10,workspace='example')
            browse.close_view(a)
            self.assertIsNone(browse.view(a));self.assertIsNone(browse.view(b))
            self.assertIn('disconnect:all',disconnect.call_args.args[0])
            release.assert_called_once_with('example')

    def test_failed_screen_disconnect_keeps_human_ownership(self):
        from herald import browser_workspaces as bw
        with mock.patch.object(bw,'take_control'),mock.patch.object(bw,'ports',return_value={'vnc':5994,'display':94}),mock.patch.object(browse,'vnc_dir',return_value=self.tmp/'vnc'),mock.patch.object(browse,'vnc_enabled',return_value=True),mock.patch.object(browse.subprocess,'run',return_value=mock.Mock(returncode=1)),mock.patch.object(bw,'release_control') as release:
            a=browse.new_view('one','sign in',10,workspace='example')
            with self.assertRaises(RuntimeError):browse.close_view(a)
            release.assert_not_called()


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
