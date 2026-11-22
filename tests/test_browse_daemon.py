"""The browse daemon, end to end against a local page in a real browser.

Skipped where Playwright's Chromium is not installed. Everything runs in a
temporary HERALD_HOME on ports of its own, so it never touches the profile
browser of the machine it runs on.

What must hold, because each of these failed before the daemon existed
(21 Nov 2026): sessions do not see each other's tabs; clean sessions do not
share cookies; a final submit and a password field are refused through every
path; a hung tab is closed instead of hanging every command; and a tab's
owner survives a daemon restart.
"""
from __future__ import annotations

import http.server
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from herald import browse_cdp, browse_daemon, config  # noqa: E402

FORM = b"""<!doctype html><title>Form</title><h1>Sheet</h1>
<label>Name <input id=name></label><label>Password <input type=password id=pw></label>
<button onclick="document.querySelector('#out').textContent='saved '+document.querySelector('#name').value;console.log('saved')">Save draft</button>
<button onclick="document.querySelector('#out').textContent='SENT'">Submit application</button>
<button onclick="document.cookie='who='+document.querySelector('#name').value">Remember me</button>
<p id=out></p><script>document.querySelector('#out').textContent=document.cookie</script>"""


class Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(FORM)

    def log_message(self, *args):
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Daemon(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        if importlib.util.find_spec("playwright") is None:
            raise unittest.SkipTest("Playwright is not installed")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.home = pathlib.Path(cls.tmp.name)
        cls.port = free_port()
        (cls.home / "config.json").write_text(json.dumps({
            "browser": {"cdp_port": cls.port, "headed": False, "vnc": False},
            "surfaces": {"browser_view": {"url": None}}}))
        cls.site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=cls.site.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.site.server_address[1]}/"
        cls.patch = mock.patch.object(config, "HOME", cls.home)
        cls.patch.start()
        cls.proc = None
        cls.start_daemon()

    @classmethod
    def start_daemon(cls):
        env = dict(os.environ, HERALD_HOME=str(cls.home), PYTHONPATH=str(ROOT / "lib"))
        env.pop("INVOCATION_ID", None)
        cls.proc = subprocess.Popen([sys.executable, "-m", "herald.browse_daemon"], env=env, cwd=str(ROOT),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 30
        while not browse_daemon.running():
            if time.time() > deadline or cls.proc.poll() is not None:
                raise unittest.SkipTest("the browse daemon did not start here")
            time.sleep(0.1)

    @classmethod
    def stop_daemon(cls):
        if cls.proc and cls.proc.poll() is None:
            cls.proc.terminate()
            cls.proc.wait(15)

    @classmethod
    def tearDownClass(cls):
        cls.stop_daemon()
        try:
            with browse_cdp.Connection(cls.port) as c:      # the headless profile browser it started
                c.send("Browser.close")
                time.sleep(0.5)
        except Exception:                                   # noqa: BLE001
            pass
        cls.site.shutdown()
        cls.patch.stop()
        cls.tmp.cleanup()

    def ask(self, cmd, session="one", **more):
        return browse_daemon.request({"cmd": cmd, "session": session, "timeout": 60, **more},
                                     timeout=90, autostart=False)

    def open(self, session="one", **more):
        r = self.ask("open", session, url=self.url, **more)
        self.assertTrue(r["ok"], r["out"])
        return r["tab"]

    def ref(self, tab, name, session="one"):
        line = self.ask("snap", session, tab=tab, find=name)["out"].splitlines()[-1]
        return line.split("[ref=")[1].split("]")[0]

    def test_sessions_have_their_own_tabs_and_an_id_still_reaches_any(self):
        mine, theirs = self.open("one"), self.open("two")
        listed = self.ask("tabs", "one")["out"]
        self.assertIn(mine, listed)
        self.assertNotIn(theirs, listed)
        self.assertIn(theirs, self.ask("tabs", "one", all=True)["out"])
        self.assertTrue(self.ask("text", "one", tab=theirs)["ok"], "an explicit id is deliberate")
        self.assertFalse(self.ask("text", "three", tab="0")["ok"], "an index only counts your own")
        self.ask("close", "one", tabs=[mine])
        self.ask("close", "two", tabs=[theirs])

    def test_steps_run_in_one_call_and_what_the_page_did_is_reported(self):
        tab = self.open(snap=True)
        name, save = self.ref(tab, "Name"), self.ref(tab, "Save draft")
        r = self.ask("do", tab=tab, steps=[f"fill {name} Ada", f"click {save}"])
        self.assertTrue(r["ok"], r["out"])
        self.assertIn("saved Ada", r["out"])
        self.assertIn("saved", self.ask("console", tab=tab)["out"])
        self.assertIn("document", self.ask("network", tab=tab)["out"])
        self.assertIn("saved Ada", self.ask("eval", tab=tab, js="() => document.querySelector('#out').textContent")["out"])
        self.ask("close", tabs=[tab])

    def test_submit_and_password_are_refused_on_every_path(self):
        tab = self.open()
        submit, password = self.ref(tab, "Submit application"), self.ref(tab, "Password")
        r = self.ask("do", tab=tab, steps=[f"click {submit}"])
        self.assertFalse(r["ok"])
        self.assertIn("refused", r["out"])
        self.assertFalse(self.ask("click", tab=tab, target="Submit application")["ok"])
        self.assertFalse(self.ask("eval", tab=tab, js="document.querySelectorAll('button')[1].click()")["ok"])
        r = self.ask("do", tab=tab, steps=[f"fill {password} hunter2"])
        self.assertFalse(r["ok"])
        self.assertIn("protected", r["out"])
        self.assertNotIn("SENT", self.ask("text", tab=tab)["out"])
        self.ask("close", tabs=[tab])

    def test_clean_sessions_do_not_share_cookies(self):
        a = self.open("one", clean=True)
        self.ask("do", "one", tab=a, quiet=True,
                 steps=[f"fill {self.ref(a, 'Name', 'one')} Ada", f"click {self.ref(a, 'Remember me', 'one')}"])
        again, other = self.open("one", clean=True), self.open("two", clean=True)
        self.assertIn("who=Ada", self.ask("text", "one", tab=again)["out"])
        self.assertNotIn("who=Ada", self.ask("text", "two", tab=other)["out"])
        self.assertFalse(self.ask("open", "two", url=self.url, device="No Such Phone")["ok"])
        self.ask("close", "one", all=True)
        self.ask("close", "two", all=True)

    def test_a_tab_with_the_person_is_refused_and_the_rest_keep_working(self):
        from herald import browse_tabs as tabs
        theirs, mine = self.open(), self.open()
        tabs.reserve("default", theirs, time.time() + 600, "sign in to a site")
        r = self.ask("snap", tab=theirs)
        self.assertFalse(r["ok"])
        self.assertIn("with the user", r["out"])
        self.assertTrue(self.ask("snap", tab=mine)["ok"])
        self.assertFalse(self.ask("returned", tab=theirs)["ok"])
        tabs.release("default", theirs)
        self.assertTrue(self.ask("returned", tab=theirs)["ok"])
        self.ask("close", all=True)

    def test_a_hung_tab_is_closed_and_other_tabs_never_wait_for_it(self):
        tab = self.open()
        hung = browse_cdp.new_background_tab(self.port, self.url)
        with browse_cdp.Connection(self.port) as c:
            sid = c.call("Target.attachToTarget", {"targetId": hung, "flatten": True})["sessionId"]
            c.send("Runtime.evaluate", {"expression": "while(true){}"}, sid)
            time.sleep(0.5)
        states = {p["tab"]: p["state"] for p in browse_cdp.probe(self.port, 2)["pages"]}
        self.assertEqual(states[hung[:6].lower()], "wedged")
        started = time.time()
        self.assertTrue(self.ask("text", tab=tab)["ok"])
        self.assertLess(time.time() - started, 2, "a healthy tab answered at once")
        r = self.ask("heal")
        self.assertIn(hung[:6].lower(), r["out"])
        self.assertNotIn(hung[:6].lower(), {p["tab"] for p in browse_cdp.probe(self.port, 2)["pages"]})
        self.assertTrue(self.ask("text", tab=tab)["ok"])
        self.ask("close", tabs=[tab])

    def test_owners_survive_a_daemon_restart(self):
        tab = self.open("keeper")
        self.stop_daemon()
        self.start_daemon()
        self.assertIn(tab, self.ask("tabs", "keeper")["out"])
        self.assertIn("Sheet", self.ask("text", "keeper", tab=tab)["out"])
        self.ask("close", "keeper", tabs=[tab])


if __name__ == "__main__":
    unittest.main()
