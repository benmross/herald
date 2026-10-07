"""A machine where systemd is installed but not running, or not installed.

WSL2 with systemd switched off and every container are this machine. Setup
has to say so once, in words, and never claim that anything is scheduled.
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lib"))

from setup import preflight, services  # noqa: E402


class SchedulerAbsent(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(services, "platform_name", return_value="systemd")
        p.start()
        self.addCleanup(p.stop)

    def _state(self, value):
        return mock.patch.object(preflight, "_systemd_state", return_value=value)

    def test_no_systemctl_is_a_note_and_not_a_traceback(self):
        with mock.patch.object(preflight.shutil, "which", return_value=None):
            result = services.install()
            self.assertEqual(result["platform"], "none")
            self.assertIn("systemd is not installed", result["note"])
            self.assertFalse(services.available())
            self.assertEqual(services.status(), [])
            self.assertFalse(services.restart("herald-telegram.service")[0])

    def test_systemd_switched_off_writes_no_units_and_gives_the_fix(self):
        off = (False, "installed but not running", "add systemd=true to /etc/wsl.conf")
        with self._state(off), \
                mock.patch.object(services, "install_systemd") as write:
            result = services.install()
        write.assert_not_called()
        self.assertEqual(result["platform"], "none")
        self.assertIn("wsl.conf", result["note"])

    def test_a_running_systemd_is_still_installed_to(self):
        with self._state((True, "running", "")), \
                mock.patch.object(services, "install_systemd", return_value=["x"]):
            self.assertEqual(services.install(),
                             {"platform": "systemd", "written": ["x"]})


class WindowsLeaksIntoWsl(unittest.TestCase):
    def test_a_windows_claude_is_not_counted_as_installed(self):
        with mock.patch.object(preflight.shutil, "which",
                               return_value="/mnt/c/Users/x/AppData/Roaming/npm/claude"):
            names = [c["name"] for c in preflight._wsl_checks(pathlib.Path("/home/x/herald"))]
        self.assertIn("Claude Code is the Linux one, not the Windows one", names)

    def test_a_checkout_on_a_windows_drive_blocks_setup(self):
        with mock.patch.object(preflight.shutil, "which", return_value="/usr/bin/claude"):
            found = preflight._wsl_checks(pathlib.Path("/mnt/c/Users/x/herald"))
        self.assertEqual([c["required"] for c in found], [True])
        self.assertIn("Linux disk", found[0]["name"])

    def test_an_ordinary_wsl_install_adds_nothing(self):
        with mock.patch.object(preflight.shutil, "which",
                               return_value="/home/x/.local/bin/claude"):
            self.assertEqual(preflight._wsl_checks(pathlib.Path("/home/x/herald")), [])


if __name__ == "__main__":
    unittest.main()
