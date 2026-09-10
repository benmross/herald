"""The config layer: where the person's data lives, and how settings merge.

Herald was one directory until 9 September 2026, and the split into program
(ROOT) and person (HOME) is the seam everything else in the open-source
restructure hangs off. These tests exist because the failure mode of getting it
wrong is silent and total: a Herald that resolves the wrong home reads an empty
ledger and cheerfully reports that nothing is happening.
"""

from __future__ import annotations

import importlib
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))


def _fresh_config(**env):
    """Import a fresh copy of herald.config under the given environment.

    Paths are module-level constants resolved at import, which is what makes
    them cheap everywhere else and what makes them need this here.
    """
    with mock.patch.dict(os.environ, env, clear=False):
        for key in ("HERALD_HOME",):
            if key not in env:
                os.environ.pop(key, None)
        import herald.config as cfg
        return importlib.reload(cfg)


class HomeResolution(unittest.TestCase):
    def tearDown(self):
        _fresh_config()          # leave the module as the rest of the suite expects

    def test_the_env_var_wins(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _fresh_config(HERALD_HOME=td)
            self.assertEqual(cfg.HOME, pathlib.Path(td).resolve())
            self.assertFalse(cfg.LEGACY_LAYOUT)
            self.assertEqual(cfg.CONFIG_PATH, cfg.HOME / "config.json")
            self.assertEqual(cfg.SECRETS_PATH, cfg.HOME / "secrets.json")

    def test_the_ledger_is_always_under_home(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _fresh_config(HERALD_HOME=td)
            self.assertEqual(cfg.LEDGER, cfg.HOME / "ledger")
            self.assertEqual(cfg.DB_PATH, cfg.HOME / "ledger" / "facts.db")

    def test_logs_stay_with_the_program(self):
        """Logs are about Herald's processes, not the person. systemd units,
        the watchdog and bin/herald-brain all name this path."""
        with tempfile.TemporaryDirectory() as td:
            cfg = _fresh_config(HERALD_HOME=td)
            self.assertEqual(cfg.LOGS, cfg.ROOT / "logs")

    def test_the_pre_split_layout_still_resolves(self):
        """The instance this was extracted from was running while it was
        extracted. A real ROOT/ledger directory (not a symlink) and no
        ~/.herald/config.json means the old paths, unchanged."""
        cfg = _fresh_config()
        ledger = cfg.ROOT / "ledger"
        if ledger.is_dir() and not ledger.is_symlink() \
                and not (pathlib.Path.home() / ".herald" / "config.json").exists():
            self.assertTrue(cfg.LEGACY_LAYOUT)
            self.assertEqual(cfg.CONFIG_PATH, cfg.ROOT / "config" / "herald.json")
        else:
            self.assertFalse(cfg.LEGACY_LAYOUT)

    def test_ensure_dirs_builds_a_home_from_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td) / "brand-new"
            cfg = _fresh_config(HERALD_HOME=str(home))
            cfg.ensure_dirs()
            for sub in ("ledger/identity", "ledger/state/areas", "ledger/journal",
                        "ledger/raw"):
                self.assertTrue((home / sub).is_dir(), sub)


class SettingsMerge(unittest.TestCase):
    def tearDown(self):
        _fresh_config()

    def _home_with(self, td, user_config: dict):
        home = pathlib.Path(td)
        (home / "config.json").write_text(json.dumps(user_config))
        return _fresh_config(HERALD_HOME=str(home))

    def test_defaults_apply_when_the_user_said_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._home_with(td, {})
            self.assertEqual(cfg.get("engines.primary.cmd"), "claude")
            self.assertEqual(cfg.get("notify.ntfy_url"), "https://ntfy.sh")

    def test_the_user_overrides_one_leaf_without_losing_its_siblings(self):
        """The reason this is a deep merge. A user who pins a model must not
        silently lose the escalate model, the permission mode and the
        timeouts that came with it."""
        with tempfile.TemporaryDirectory() as td:
            cfg = self._home_with(td, {"engines": {"primary": {"model": "opus"}}})
            self.assertEqual(cfg.get("engines.primary.model"), "opus")
            self.assertEqual(cfg.get("engines.primary.escalate_model"), "opus")
            self.assertEqual(cfg.get("engines.primary.permission_mode"), "auto")
            self.assertEqual(cfg.get("engines.think_idle_timeout_seconds"), 900)

    def test_a_list_replaces_rather_than_appends(self):
        """Someone who names three feeds means those three, not those three
        plus whatever Herald shipped."""
        with tempfile.TemporaryDirectory() as td:
            cfg = self._home_with(td, {"jobs": {"feeds": ["a", "b"]}})
            self.assertEqual(cfg.get("jobs.feeds"), ["a", "b"])

    def test_a_missing_user_config_is_not_fatal(self):
        """A fresh install runs `herald setup` before it has a config, and the
        wizard itself needs to be able to import this module."""
        with tempfile.TemporaryDirectory() as td:
            cfg = _fresh_config(HERALD_HOME=str(pathlib.Path(td) / "nope"))
            self.assertEqual(cfg.get("agent.name"), "Herald")

    def test_dotted_lookup_returns_the_default_for_an_unknown_path(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._home_with(td, {})
            self.assertIsNone(cfg.get("nothing.here"))
            self.assertEqual(cfg.get("nothing.here", "fallback"), "fallback")


if __name__ == "__main__":
    unittest.main()


class VenvReExec(unittest.TestCase):
    """`bin/herald` re-execs into the project venv, and the test for whether it
    is already there has to be sys.prefix.

    venv/bin/python is a symlink chain ending at the system interpreter, so
    comparing resolved interpreter paths said "already in the venv" while
    running under the system one -- with none of its packages. It was invisible
    for as long as everything needing them ran as a subprocess through
    config.python(); the first in-process import of googleapiclient found it.
    """

    def test_the_check_is_on_the_prefix_not_the_interpreter_path(self):
        source = (ROOT / "bin" / "herald").read_text()
        self.assertIn("pathlib.Path(sys.prefix).resolve()", source)
        self.assertNotIn("pathlib.Path(sys.executable).resolve() != _VENV_PY", source)

    def test_a_venv_python_symlinked_to_the_system_one_still_differs_by_prefix(self):
        import subprocess
        venv = ROOT / "venv" / "bin" / "python"
        if not venv.exists():
            self.skipTest("no venv here")
        prefix = subprocess.run([str(venv), "-c", "import sys; print(sys.prefix)"],
                                capture_output=True, text=True).stdout.strip()
        self.assertEqual(pathlib.Path(prefix).resolve(),
                         (ROOT / "venv").resolve())
