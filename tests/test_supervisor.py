"""The container's scheduler: when a job is due, and what the platform reports.

bin/herald-supervisor stands in for nine systemd units. A mistake in `due`
is a digest that never arrives or one that arrives twelve times.
"""

from __future__ import annotations

import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lib"))

from setup import engine, preflight, services  # noqa: E402


def _load():
    loader = importlib.machinery.SourceFileLoader(
        "supervisor_under_test", str(ROOT / "bin" / "herald-supervisor"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


AT_0630 = dt.datetime(2026, 10, 8, 6, 30, 2)


class Due(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sup = _load()

    def test_a_time_of_day_fires_once_in_its_minute(self):
        job = {"Label": "dawn", "StartCalendarInterval": [{"Hour": 6, "Minute": 30}]}
        fired: set[str] = set()
        self.assertTrue(self.sup.due(job, AT_0630, None, fired, 0))
        later = AT_0630 + dt.timedelta(seconds=5)
        self.assertFalse(self.sup.due(job, later, 0, fired, 5))

    def test_it_fires_again_the_next_day(self):
        job = {"Label": "dawn", "StartCalendarInterval": [{"Hour": 6, "Minute": 30}]}
        fired: set[str] = set()
        self.sup.due(job, AT_0630, None, fired, 0)
        self.assertTrue(self.sup.due(job, AT_0630 + dt.timedelta(days=1), 0, fired, 86400))

    def test_no_other_minute_fires_it(self):
        job = {"Label": "dawn", "StartCalendarInterval": [{"Hour": 6, "Minute": 30}]}
        self.assertFalse(self.sup.due(job, AT_0630.replace(minute=31), None, set(), 0))

    def test_an_interval_job_runs_at_start_and_then_on_its_period(self):
        job = {"Label": "collect", "StartInterval": 1800}
        self.assertTrue(self.sup.due(job, AT_0630, None, set(), 1000))
        self.assertFalse(self.sup.due(job, AT_0630, 1000, set(), 2000))
        self.assertTrue(self.sup.due(job, AT_0630, 1000, set(), 2800))

    def test_a_kept_alive_job_is_not_restarted_in_a_tight_loop(self):
        job = {"Label": "telegram", "KeepAlive": True}
        self.assertTrue(self.sup.due(job, AT_0630, None, set(), 100))
        self.assertFalse(self.sup.due(job, AT_0630, 100, set(), 105))
        self.assertTrue(self.sup.due(job, AT_0630, 100, set(), 100 + self.sup.RESTART_BACKOFF))


class ContainerPlatform(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        patches = [
            mock.patch.dict(os.environ, {"HERALD_CONTAINER": "1"}),
            mock.patch.object(services, "supervisor_state_path",
                              return_value=self.tmp / "supervisor.json"),
            mock.patch.object(services, "supervisor_marker",
                              return_value=self.tmp / "supervisor.enabled"),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    def _ticking(self, jobs=None):
        (self.tmp / "supervisor.json").write_text(json.dumps(
            {"pid": 1, "ts": time.time(), "enabled": True, "jobs": jobs or {}}))

    def test_the_platform_is_the_container_and_wsl_checks_stand_down(self):
        self.assertEqual(services.platform_name(), "container")
        with mock.patch.object(preflight.platform, "release",
                               return_value="6.6.87.2-microsoft-standard-WSL2"):
            self.assertFalse(preflight.on_wsl())

    def test_setup_listeners_bind_every_interface_only_in_the_container(self):
        self.assertEqual(engine.listen_host(), "0.0.0.0")
        with mock.patch.dict(os.environ, {"HERALD_CONTAINER": ""}):
            self.assertEqual(engine.listen_host(), "127.0.0.1")

    def test_without_a_supervisor_nothing_is_claimed(self):
        self.assertIn("supervisor is not running", services.unavailable_reason())
        self.assertEqual(services.install()["platform"], "none")
        self.assertFalse((self.tmp / "supervisor.enabled").exists())

    def test_install_switches_the_supervisor_on_and_says_so_once(self):
        self._ticking()
        self.assertEqual(services.install()["written"], ["the supervisor's schedule"])
        self.assertTrue((self.tmp / "supervisor.enabled").exists())
        self.assertEqual(services.install()["written"], [])

    def test_status_reads_what_the_supervisor_wrote(self):
        self._ticking({"com.herald.telegram": {"state": "active", "pid": 9},
                       "com.herald.dawn": {"state": "loaded", "pid": None}})
        self.assertEqual(dict(services.status()),
                         {"com.herald.telegram": "active", "com.herald.dawn": "loaded"})

    def test_a_stale_state_file_is_no_supervisor(self):
        (self.tmp / "supervisor.json").write_text(json.dumps(
            {"pid": 1, "ts": time.time() - 600, "jobs": {"x": {"state": "active"}}}))
        self.assertEqual(services.status(), [])

    def test_restart_signals_the_job_and_leaves_the_supervisor_to_start_it(self):
        self._ticking({"com.herald.telegram": {"state": "active", "pid": 4242}})
        with mock.patch.object(services.os, "kill") as kill:
            self.assertEqual(services.restart("herald-telegram.service"), (True, ""))
        kill.assert_called_once_with(4242, 15)

    def test_the_bridge_is_only_a_job_once_there_is_a_bot(self):
        with mock.patch.object(services.config, "secret", return_value=None):
            self.assertNotIn("com.herald.telegram", services.container_jobs())
        with mock.patch.object(services.config, "secret", return_value="token"):
            self.assertIn("com.herald.telegram", services.container_jobs())


if __name__ == "__main__":
    unittest.main()
