"""The container healthcheck: what makes Docker say unhealthy, and what does not.

A healthcheck that only asked "is the supervisor ticking" reported healthy
while the Telegram bridge crash-looped every fifteen seconds. These pin the
cases that matter: idle before setup is fine, a stale heartbeat or a
crash-looping kept-alive job is not, and a warning never flips the result.
"""

from __future__ import annotations

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

from herald import health  # noqa: E402
from setup import services  # noqa: E402

BRIDGE = "com.herald.telegram"


class Supervisor(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        patches = [
            mock.patch.dict(os.environ, {"HERALD_CONTAINER": "1"}),
            mock.patch.object(services, "supervisor_state_path",
                              return_value=self.tmp / "supervisor.json"),
            mock.patch.object(services, "container_jobs",
                              return_value={BRIDGE: {"KeepAlive": True}, "com.herald.dawn": {}}),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    def _state(self, ts=None, enabled=True, jobs=None):
        (self.tmp / "supervisor.json").write_text(json.dumps(
            {"ts": time.time() if ts is None else ts, "enabled": enabled, "jobs": jobs or {}}))

    def _checks(self):
        checks, _ = health._supervisor(True)
        return checks

    def test_no_heartbeat_is_unhealthy(self):
        self.assertFalse(health.healthy(self._checks()))

    def test_a_stale_heartbeat_is_unhealthy(self):
        self._state(ts=time.time() - 600)
        self.assertFalse(health.healthy(self._checks()))

    def test_idle_before_setup_is_healthy_and_skips_the_rest(self):
        self._state(enabled=False)
        checks, enabled = health._supervisor(True)
        self.assertTrue(health.healthy(checks))
        self.assertFalse(enabled)

    def test_a_running_bridge_is_healthy(self):
        now = time.time()
        self._state(jobs={BRIDGE: {"state": "active", "last_start": now - 3600, "starts": [now - 3600]}})
        self.assertTrue(health.healthy(self._checks()))

    def test_a_crash_looping_bridge_is_unhealthy_even_while_momentarily_up(self):
        now = time.time()
        self._state(jobs={BRIDGE: {"state": "active", "last_start": now - 2,
                                   "starts": [now - 60, now - 45, now - 30, now - 2]}})
        checks = self._checks()
        self.assertFalse(health.healthy(checks))
        self.assertIn("restarted 4 times", next(c.detail for c in checks if c.name == "telegram"))

    def test_scheduled_jobs_that_are_not_running_are_normal(self):
        self._state(jobs={"com.herald.dawn": {"state": "loaded", "last_start": None}})
        self.assertTrue(health.healthy(self._checks()))


class Result(unittest.TestCase):
    def test_a_warning_never_makes_it_unhealthy(self):
        checks = [health.Check("database", True), health.Check("failing sources", False, "x", fatal=False)]
        self.assertTrue(health.healthy(checks))
        self.assertIn("warn  failing sources", health.report(checks))
        self.assertTrue(health.report(checks).endswith("healthy"))

    def test_one_fatal_failure_is_unhealthy(self):
        checks = [health.Check("database", True), health.Check("disk", False, "10 MB free")]
        self.assertFalse(health.healthy(checks))
        self.assertTrue(health.report(checks).endswith("unhealthy"))


if __name__ == "__main__":
    unittest.main()
