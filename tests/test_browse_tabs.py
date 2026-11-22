"""Tab ownership: the rules that let parallel agents share one browser.

What must hold: a session's name comes from where the command runs, so two
agents do not have to choose one; a tab is closed when its owner is done with
it and never while something says it must stay; a tab the person is driving is
theirs; and one tab has one writer at a time, across processes.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))
from herald import browse_tabs as tabs, config  # noqa: E402


class Names(unittest.TestCase):
    def name(self, **env):
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("HERALD_BROWSE_SESSION", "HERALD_TELEGRAM_TOPIC", "CLAUDE_CODE_SESSION_ID",
                              "CODEX_THREAD_ID", "CODEX_SESSION_ID", "KIRO_SESSION_ID")}
        with mock.patch.dict(os.environ, {**clean, **env}, clear=True):
            return tabs.session_name(env.pop("explicit", None) if "explicit" in env else None)

    def test_a_topic_keeps_its_tabs_between_messages(self):
        self.assertEqual(self.name(HERALD_TELEGRAM_TOPIC="-100123:456", CLAUDE_CODE_SESSION_ID="abcdef0123"),
                         "topic-456")

    def test_agents_outside_telegram_are_told_apart_by_conversation(self):
        self.assertEqual(self.name(CLAUDE_CODE_SESSION_ID="abcdef01-2345"), "agent-abcdef01")
        self.assertEqual(self.name(), "terminal")

    def test_an_explicit_name_wins_and_is_made_safe(self):
        self.assertEqual(self.name(HERALD_BROWSE_SESSION="Worker #2", HERALD_TELEGRAM_TOPIC="1:2"), "worker-2")
        with mock.patch.dict(os.environ, {"HERALD_BROWSE_SESSION": "env"}):
            self.assertEqual(tabs.session_name("Flag"), "flag")


class Registry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = mock.patch.object(config, "HOME", pathlib.Path(self.tmp.name))
        self.home.start()

    def tearDown(self):
        self.home.stop()
        self.tmp.cleanup()

    def age(self, tab, hours):
        with tabs._db() as c:
            c.execute("UPDATE tabs SET used=used-? WHERE tab=?", (hours * 3600, tab))

    def test_each_session_sees_its_own_tabs(self):
        tabs.claim("default", "AAAAAA1111", "one")
        tabs.claim("default", "BBBBBB2222", "two")
        tabs.claim("clean", "CCCCCC3333", "one")
        self.assertEqual([r["tab"] for r in tabs.rows(session="one")], ["aaaaaa", "cccccc"])
        self.assertEqual(tabs.get("bbbbbb")["session"], "two")
        self.assertEqual(tabs.get("cccccc")["workspace"], "clean")

    def test_idle_tabs_expire_and_used_ones_do_not(self):
        tabs.claim("default", "AAAAAA1111", "one")
        tabs.claim("default", "BBBBBB2222", "one")
        self.age("aaaaaa", 7)
        self.age("bbbbbb", 7)
        tabs.touch("default", "bbbbbb", "https://example.org/")
        self.assertEqual([r["tab"] for r in tabs.expired("default")], ["aaaaaa"])

    def test_nobodys_tabs_go_sooner_than_owned_ones(self):
        tabs.sync("default", {"AAAAAA1111": "https://a.example/", "BBBBBB2222": "https://b.example/"})
        tabs.claim("default", "BBBBBB2222", "one")
        for tab in ("aaaaaa", "bbbbbb"):
            self.age(tab, 3)
        self.assertEqual([r["tab"] for r in tabs.expired("default")], ["aaaaaa"])

    def test_what_must_stay_stays(self):
        for target in ("AAAAAA1", "BBBBBB2", "CCCCCC3", "DDDDDD4"):
            tabs.claim("default", target, "one")
            self.age(target[:6].lower(), 30)
        tabs.keep("default", "aaaaaa", 48)                              # asked to stay
        tabs.reserve("default", "bbbbbb", time.time() + 600, "sign in")   # with the person
        gone = [r["tab"] for r in tabs.expired("default", protected={"cccccc"})]   # an application draft
        self.assertEqual(gone, ["dddddd"])
        tabs.keep("default", "aaaaaa", 0)
        self.assertIn("aaaaaa", [r["tab"] for r in tabs.expired("default")])

    def test_a_dead_process_loses_its_tabs_and_a_live_one_keeps_them(self):
        tabs.claim("default", "AAAAAA1111", "process-1", pid=os.getpid())
        tabs.claim("default", "BBBBBB2222", "process-2", pid=2 ** 22 + 12345)
        for tab in ("aaaaaa", "bbbbbb"):
            self.age(tab, 1)
        self.assertEqual([r["tab"] for r in tabs.expired("default")], ["bbbbbb"])

    def test_sync_follows_the_browser(self):
        tabs.claim("default", "AAAAAA1111", "one")
        tabs.sync("default", {"BBBBBB2222": "https://b.example/"})
        self.assertIsNone(tabs.get("aaaaaa"))
        self.assertEqual(tabs.get("bbbbbb")["session"], "")

    def test_a_silent_tab_is_struck_and_an_answer_clears_it(self):
        tabs.claim("default", "AAAAAA1111", "one")
        self.assertEqual(tabs.strike("default", "aaaaaa"), 1)
        self.assertEqual(tabs.strike("default", "aaaaaa"), 2)
        tabs.touch("default", "aaaaaa")
        self.assertEqual(tabs.strike("default", "aaaaaa"), 1)

    def test_one_writer_per_tab(self):
        tabs.claim("default", "AAAAAA1111", "one")
        with tabs.lock("default", "aaaaaa", timeout=0.1):
            self.assertIsNone(tabs.try_lock("default", "aaaaaa"))
            with self.assertRaises(tabs.TabBusy) as busy:
                with tabs.lock("default", "aaaaaa", timeout=0.1):
                    pass
            self.assertIn(str(os.getpid()), str(busy.exception))
            other = tabs.try_lock("default", "bbbbbb")
            self.assertIsNotNone(other, "another tab is free")
            os.close(other)
        fd = tabs.try_lock("default", "aaaaaa")
        self.assertIsNotNone(fd)
        os.close(fd)


if __name__ == "__main__":
    unittest.main()
