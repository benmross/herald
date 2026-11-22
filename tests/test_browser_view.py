"""The page a sign-in link opens: bin/herald-browser's HTTP side.

What must hold: only a live token gets anything; the page asks the browse
daemon for what it needs from a tab and keeps no browser connection of its
own; and Done gives the tab back to the agents.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from herald import browse, browse_tabs, config  # noqa: E402


def load_host():
    loader = importlib.machinery.SourceFileLoader("herald_browser_host", str(ROOT / "bin" / "herald-browser"))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


class Screen:
    clip = (820, 1180)

    def resize(self, w, h):
        self.clip = (w, h)


class View(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = pathlib.Path(self.tmp.name)
        self.host = load_host()
        self.asked = []

        def daemon(req, **_):
            self.asked.append(req)
            return {"ok": True, "out": "ok", "jpeg": "/9j/"}

        self.patches = [mock.patch.object(config, "HOME", home),
                        mock.patch.object(config, "link_ledger", return_value=None),
                        mock.patch.object(config, "LEDGER", home / "ledger"),
                        mock.patch.object(config, "DB_PATH", home / "facts.db"),
                        mock.patch.object(browse, "vnc_enabled", return_value=False),
                        mock.patch.object(self.host.browse_daemon, "request", daemon),
                        mock.patch.object(self.host, "CHROME", Screen())]
        for p in self.patches:
            p.start()
        self.server = HTTPServer(("127.0.0.1", 0), self.host.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.token = browse.new_view("abc123", "sign in to a site", 10, workspace="default")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def call(self, path, body=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()

    def test_only_a_live_token_is_served(self):
        self.assertEqual(self.call(f"/v/{self.token}/live")[0], 200)
        self.assertEqual(self.call("/v/" + "A" * 32 + "/live")[0], 404)
        self.assertEqual(self.call("/anything")[0], 404)
        self.assertEqual(self.call("/v/" + "A" * 32 + "/act", {"k": "done"})[0], 404)

    def test_the_live_page_has_the_reason_and_a_way_to_finish(self):
        status, body = self.call(f"/v/{self.token}/live")
        self.assertEqual(status, 200)
        self.assertIn(b"sign in to a site", body)
        self.assertIn(b"id=done", body)

    def test_sizing_asks_the_daemon_to_fit_and_raise_that_tab(self):
        self.assertEqual(self.call(f"/v/{self.token}/size", {"w": 400, "h": 700})[0], 200)
        asked = self.asked[-1]
        self.assertEqual((asked["cmd"], asked["op"], asked["tab"]), ("human", "bounds", "abc123"))
        self.assertEqual((asked["w"], asked["h"]), (400, 700))

    def test_taps_go_to_the_tab_the_link_is_for_and_nothing_else_is_accepted(self):
        self.assertEqual(self.call(f"/v/{self.token}/act", {"k": "click", "x": 10, "y": 20})[0], 200)
        self.assertEqual((self.asked[-1]["op"], self.asked[-1]["tab"]), ("click", "abc123"))
        self.assertEqual(self.call(f"/v/{self.token}/act", {"k": "key", "key": "F12"})[0], 400)
        self.assertEqual(self.call(f"/v/{self.token}/act", {"k": "eval", "js": "1"})[0], 400)

    def test_done_gives_the_tab_back(self):
        self.assertTrue(browse_tabs.with_person(browse_tabs.get("abc123", "default")))
        self.assertEqual(self.call(f"/v/{self.token}/act", {"k": "done"})[0], 200)
        self.assertFalse(browse_tabs.with_person(browse_tabs.get("abc123", "default")))
        self.assertEqual(self.call(f"/v/{self.token}/live")[0], 404)


if __name__ == "__main__":
    unittest.main()
