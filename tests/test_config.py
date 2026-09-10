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


class PersonalDataCheck(unittest.TestCase):
    """`tools/check.py` refuses to let anything personal into the public tree,
    and the account name is the needle that most easily cries wolf.

    The public repository's very first CI run failed on three innocent lines --
    `${{ runner.temp }}`, "the test runner's PYTHONPATH", "the bundled runner"
    -- because the CI machine's login is `runner`. A check that fails on prose
    is a check somebody switches off, and a red first build is what a stranger
    sees before they read a line of the code.
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "herald_check", ROOT / "tools" / "check.py")
        cls.check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.check)

    # The fixtures below are assembled rather than written out, and that is not
    # style. A home path spelt out in full here is, on a machine whose login is
    # the one being tested, exactly the string the check exists to catch -- so
    # writing the test the obvious way makes the test itself fail the check.
    # That is how the second CI run went red, and the comment explaining it
    # went red the same way. Do not "simplify" these back.
    LOGIN = "runner"

    def test_a_home_path_is_a_leak(self):
        p = self.check.account_pattern(self.LOGIN)
        self.assertTrue(p.search(f"/home/{self.LOGIN}/.herald/extensions/private-thing"))
        self.assertTrue(p.search(f"/Users/{self.LOGIN}/.herald".lower()))
        self.assertTrue(p.search(f"~{self.LOGIN}/notes"))
        self.assertTrue(p.search(f"scp {self.LOGIN}@box:/tmp/x ."))

    def test_a_system_path_that_happens_to_share_the_login_is_not(self):
        """A login called `dev` must not flag /dev/null, and one called `bin`
        must not flag every shebang in the tree. Only home directories and
        addresses count; anything else is covered by the literal home-path
        needles the caller builds separately."""
        for login, innocent in (("dev", "cmd >/dev/null 2>&1"),
                                ("dev", "read -r reply </dev/tty"),
                                ("dev", "Summer2027-Internships/dev/.github/x.json"),
                                ("bin", "#!/usr/bin/env python"),
                                ("tmp", "written to /tmp/herald-probe")):
            p = self.check.account_pattern(login)
            self.assertIsNone(p.search(innocent), f"{login}: {innocent}")

    def test_the_same_word_in_prose_is_not(self):
        p = self.check.account_pattern(self.LOGIN)
        for innocent in ("${{ runner.temp }}/herald-home",
                         "let the test runner's PYTHONPATH supply the program",
                         "Use the bundled runner. It points Python at",
                         "a runner-up"):
            self.assertIsNone(p.search(innocent.lower()), innocent)

    def test_a_two_letter_login_still_only_matches_paths(self):
        """`pi` is the default login on a Raspberry Pi, and appears inside
        every other word in the language."""
        short = "pi"
        p = self.check.account_pattern(short)
        self.assertTrue(p.search(f"/home/{short}/.herald"))
        for innocent in ("pipeline", "the api", "pip install", "copying"):
            self.assertIsNone(p.search(innocent), innocent)


class LedgerSymlink(unittest.TestCase):
    """`ROOT/ledger` -> `$HERALD_HOME/ledger` is the most load-bearing thing in
    the layout: every prompt, skill and docstring says `ledger/identity/...`,
    relative to the checkout.

    It existed on the first install because it was made by hand during the
    split, and no code path created it until a rehearsal of a fresh install
    fell over on exactly that. These tests are why it cannot go missing again.
    """

    def tearDown(self):
        _fresh_config()

    def test_a_fresh_install_gets_the_link(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td) / "home"
            root = pathlib.Path(td) / "program"
            root.mkdir()
            cfg = _fresh_config(HERALD_HOME=str(home))
            cfg.ROOT = root
            cfg.LEDGER = home / "ledger"
            (home / "ledger").mkdir(parents=True)
            self.assertIn("linked", cfg.link_ledger() or "")
            link = root / "ledger"
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), (home / "ledger").resolve())

    def test_it_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td) / "home"
            root = pathlib.Path(td) / "program"
            root.mkdir()
            (home / "ledger").mkdir(parents=True)
            cfg = _fresh_config(HERALD_HOME=str(home))
            cfg.ROOT = root
            cfg.LEDGER = home / "ledger"
            cfg.link_ledger()
            self.assertIsNone(cfg.link_ledger())

    def test_a_stale_link_is_repointed(self):
        """A home that moved leaves a link into a directory that is gone."""
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td) / "home"
            root = pathlib.Path(td) / "program"
            root.mkdir()
            (home / "ledger").mkdir(parents=True)
            (root / "ledger").symlink_to(pathlib.Path(td) / "old" / "ledger")
            cfg = _fresh_config(HERALD_HOME=str(home))
            cfg.ROOT = root
            cfg.LEDGER = home / "ledger"
            self.assertIn("repointed", cfg.link_ledger() or "")
            self.assertEqual((root / "ledger").resolve(), (home / "ledger").resolve())

    def test_a_real_directory_is_never_clobbered(self):
        """Data in the wrong place is still data. Report it; do not delete it."""
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td) / "home"
            root = pathlib.Path(td) / "program"
            (root / "ledger").mkdir(parents=True)
            (root / "ledger" / "keepme.md").write_text("mine")
            (home / "ledger").mkdir(parents=True)
            cfg = _fresh_config(HERALD_HOME=str(home))
            cfg.ROOT = root
            cfg.LEDGER = home / "ledger"
            self.assertIn("real directory", cfg.link_ledger() or "")
            self.assertTrue((root / "ledger" / "keepme.md").exists())
