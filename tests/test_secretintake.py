"""Secret intake: a value pasted on the phone reaches secrets.json and nothing
else (16 Oct 2026).

What must hold: a link works once and expires; the spool never holds the value
or the token; a path that would wipe sibling secrets is refused; every stored
value leaves an actions row.
"""

from __future__ import annotations

import contextlib
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import config, db, secretintake  # noqa: E402


class Intake(unittest.TestCase):
    def setUp(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.secrets = tmp / "secrets.json"
        self.secrets.write_text(json.dumps({"telegram": {"bot_token": "x", "chat": 1}}))
        self.con = sqlite3.connect(":memory:")
        self.con.execute("CREATE TABLE actions (id INTEGER PRIMARY KEY, ts, actor, tier,"
                         " kind, target, summary, ref, approval_id, reported_at)")

        @contextlib.contextmanager
        def session():
            yield self.con

        cfg = {"surfaces.secret_intake.url": "https://box.example:8448/",
               "surfaces.secret_intake.port": 8792}
        for target, attr, value in (
                (config, "HOME", tmp), (config, "SECRETS_PATH", self.secrets),
                (config, "_secrets", None), (db, "session", session),
                (config, "get", lambda k, d=None: cfg.get(k, d))):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def _token(self, url):
        return url.rsplit("/", 1)[1]

    def test_round_trip(self):
        req, url = secretintake.request("openai.api_key", why="for the scout")
        self.assertTrue(url.startswith("https://box.example:8448/s/"))
        token = self._token(url)
        secretintake.store(token, "  sk-abc\n")
        self.assertEqual(json.loads(self.secrets.read_text())["openai"]["api_key"], "sk-abc")
        self.assertEqual(json.loads(self.secrets.read_text())["telegram"]["chat"], 1)
        self.assertIsNone(secretintake.find(token), "a link must work once")
        row = self.con.execute("SELECT tier, kind, target FROM actions").fetchone()
        self.assertEqual(row, ("green", "secret.store", "secrets.openai.api_key"))

    def test_spool_holds_neither_value_nor_token(self):
        _, url = secretintake.request("openai.api_key", why="x")
        token = self._token(url)
        secretintake.store(token, "sk-very-secret")
        blob = "".join(f.read_text() for f in secretintake.spool().glob("*"))
        self.assertNotIn("sk-very-secret", blob)
        self.assertNotIn(token, blob)

    def test_expired(self):
        _, url = secretintake.request("a.b", why="x", minutes=1)
        with mock.patch.object(secretintake, "_now", return_value=10**11):
            self.assertIsNone(secretintake.find(self._token(url)))
            with self.assertRaises(LookupError):
                secretintake.store(self._token(url), "v")

    def test_refuses_paths_that_would_clobber(self):
        for bad in ("telegram", "telegram.bot_token.inner", "../x", "a b", ""):
            with self.assertRaises(ValueError, msg=bad):
                secretintake.request(bad, why="x")

    def test_json(self):
        _, url = secretintake.request("google.sa", why="x", as_json=True)
        with self.assertRaises(ValueError):
            secretintake.store(self._token(url), "not json")
        secretintake.store(self._token(url), '{"k": 1}')
        self.assertEqual(json.loads(self.secrets.read_text())["google"]["sa"], {"k": 1})

    def test_unknown_token(self):
        self.assertIsNone(secretintake.find("0" * 48))
        self.assertIsNone(secretintake.find("../../secrets"))


if __name__ == "__main__":
    unittest.main()
