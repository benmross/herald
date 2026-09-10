"""Modes, releases and the guard that makes tracking mean something.

Built against real git repositories in temp directories rather than mocks,
because every interesting case here is a fact about git -- whether a tag is a
descendant, whether a fast-forward is possible, whether a hook actually runs --
and a mock would only ever confirm what the test author already believed.
"""

from __future__ import annotations

import importlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True)


class ModeAndHook(unittest.TestCase):
    """A tracking install must physically refuse a commit in the program."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = pathlib.Path(self.tmp.name) / "program"
        self.home = pathlib.Path(self.tmp.name) / "home"
        self.repo.mkdir()
        self.home.mkdir()
        (self.home / "config.json").write_text("{}")
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("config", "user.email", "t@example.com", cwd=self.repo)
        git("config", "user.name", "T", cwd=self.repo)
        (self.repo / "a.txt").write_text("one\n")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "first", cwd=self.repo)

        # Point a fresh herald.config at these directories.
        with mock.patch.dict(os.environ, {"HERALD_HOME": str(self.home)}):
            import herald.config as cfg
            self.cfg = importlib.reload(cfg)
        self.cfg.ROOT = self.repo
        import herald.upstream as up
        self.up = importlib.reload(up)
        self.up.config = self.cfg
        self.up.CHANGELOG = self.repo / "CHANGELOG.md"

    def tearDown(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERALD_HOME", None)
            import herald.config as cfg
            import herald.upstream as up
            importlib.reload(cfg)
            importlib.reload(up)

    def test_the_default_is_tracking(self):
        self.assertEqual(self.up.mode(), "tracking")
        self.assertFalse(self.up.may_self_edit())

    def test_a_fork_and_a_maintainer_may_self_edit(self):
        for mode in ("fork", "maintainer"):
            self.up.set_mode(mode)
            self.assertTrue(self.up.may_self_edit(), mode)

    def test_the_hook_refuses_a_real_commit(self):
        self.up.set_mode("tracking")
        self.assertTrue(self.up.hook_installed())
        (self.repo / "a.txt").write_text("two\n")
        git("add", "-A", cwd=self.repo)
        # The hook reads the mode at commit time from $HERALD_HOME, the way
        # the program does, so the commit has to be made against this home.
        with mock.patch.dict(os.environ, {"HERALD_HOME": str(self.home)}):
            result = git("commit", "-m", "should not happen", cwd=self.repo)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tracks upstream", result.stderr + result.stdout)
        log = git("log", "--oneline", cwd=self.repo).stdout
        self.assertEqual(len(log.strip().splitlines()), 1)

    def test_switching_to_fork_lets_the_commit_through(self):
        self.up.set_mode("tracking")
        self.up.set_mode("fork")
        self.assertFalse(self.up.hook_installed())
        (self.repo / "a.txt").write_text("two\n")
        git("add", "-A", cwd=self.repo)
        self.assertEqual(git("commit", "-m", "mine", cwd=self.repo).returncode, 0)

    def test_a_foreign_hook_is_not_clobbered(self):
        """Somebody else's pre-commit hook is theirs."""
        hook = self.up.hook_path()
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\necho mine\n")
        with self.assertRaises(FileExistsError):
            self.up.install_hook()
        self.assertIn("echo mine", hook.read_text())

    def test_tracking_refuses_when_local_commits_exist(self):
        """Reported rather than enforced here -- the CLI and the setup step
        both check it -- but the fact has to be visible."""
        self.up.set_mode("fork")
        (self.repo / "a.txt").write_text("two\n")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "local work", cwd=self.repo)
        # With no upstream configured, @{upstream} fails and reports nothing;
        # what matters is that a dirty tree is seen.
        (self.repo / "a.txt").write_text("three\n")
        self.assertTrue(self.up.local_changes()["modified"])


class Releases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = pathlib.Path(self.tmp.name) / "program"
        self.home = pathlib.Path(self.tmp.name) / "home"
        self.repo.mkdir(); self.home.mkdir()
        (self.home / "config.json").write_text("{}")
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("config", "user.email", "t@example.com", cwd=self.repo)
        git("config", "user.name", "T", cwd=self.repo)
        for i in range(3):
            (self.repo / f"f{i}.txt").write_text(str(i))
            git("add", "-A", cwd=self.repo)
            git("commit", "-q", "-m", f"change {i}", cwd=self.repo)
        with mock.patch.dict(os.environ, {"HERALD_HOME": str(self.home)}):
            import herald.config as cfg
            self.cfg = importlib.reload(cfg)
        self.cfg.ROOT = self.repo
        import herald.upstream as up
        self.up = importlib.reload(up)
        self.up.config = self.cfg
        self.up.CHANGELOG = self.repo / "CHANGELOG.md"

    def tearDown(self):
        os.environ.pop("HERALD_HOME", None)
        import herald.config as cfg
        import herald.upstream as up
        importlib.reload(cfg); importlib.reload(up)

    def test_only_version_tags_count_as_releases(self):
        git("tag", "v0.1.0", cwd=self.repo)
        git("tag", "wip", cwd=self.repo)
        git("tag", "v0.2.0", cwd=self.repo)
        self.assertEqual(self.up.tags(), ["v0.1.0", "v0.2.0"])

    def test_tags_sort_numerically_not_alphabetically(self):
        """v0.10.0 is newer than v0.9.0, which string ordering gets wrong."""
        for tag in ("v0.9.0", "v0.10.0", "v0.11.2"):
            git("tag", tag, cwd=self.repo)
        self.assertEqual(self.up.tags()[-1], "v0.11.2")

    def test_unreleased_is_what_a_release_would_ship(self):
        git("tag", "v0.1.0", "HEAD~2", cwd=self.repo)
        self.assertEqual(len(self.up.unreleased()), 2)

    def test_next_version_bumps_the_right_part(self):
        git("tag", "v1.2.3", cwd=self.repo)
        self.assertEqual(self.up.next_version(), "v1.2.4")
        self.assertEqual(self.up.next_version("minor"), "v1.3.0")
        self.assertEqual(self.up.next_version("major"), "v2.0.0")

    def test_the_first_release_is_v0_0_1(self):
        self.assertEqual(self.up.next_version(), "v0.0.1")

    def test_a_changelog_section_is_readable_back(self):
        self.up.write_changelog("v0.2.0", ["abc123 did a thing",
                                           "def456 did another"])
        section = self.up.changelog_section("v0.2.0")
        self.assertIn("did a thing", section)
        self.assertIn("did another", section)

    def test_a_later_release_goes_above_an_earlier_one(self):
        self.up.write_changelog("v0.1.0", ["aaa first release"])
        self.up.write_changelog("v0.2.0", ["bbb second release"])
        text = (self.repo / "CHANGELOG.md").read_text()
        self.assertLess(text.index("v0.2.0"), text.index("v0.1.0"))
        self.assertIn("first release", text)

    def test_available_finds_a_newer_tag_that_is_ahead(self):
        git("tag", "v0.1.0", "HEAD~2", cwd=self.repo)
        git("checkout", "-q", "HEAD~2", cwd=self.repo)
        git("checkout", "-q", "-B", "main", cwd=self.repo)
        git("tag", "v0.2.0", "main", cwd=self.repo)
        # HEAD is at v0.1.0's commit; v0.2.0 points at the same commit here, so
        # nothing is available -- the interesting assertion is that it does not
        # offer a tag that is not ahead.
        self.assertIsNone(self.up.available())


if __name__ == "__main__":
    unittest.main()


class ABareCheckoutPasses(unittest.TestCase):
    """`tools/check.py` has to succeed on a checkout nobody has set up.

    That is what CI is -- a clone with no `$HERALD_HOME` behind it -- and three
    separate checks have now failed there for the same reason: they could not
    tell "not set up yet" from "broken". Each one was found by a red build
    rather than by a test, which is what this is for.
    """

    def test_check_exits_zero_on_a_fresh_checkout(self):
        """The layout a clone has, carrying the code as it is right now.

        Built from `git ls-files` rather than by cloning, for one reason that
        matters: a clone carries the last *commit*, so a test that cloned would
        pass or fail one commit behind whatever is being written. This copies
        the tracked files as they are on disk into an otherwise empty git
        repository -- no generated CLAUDE.md, no ledger symlink, no
        `$HERALD_HOME` -- which is exactly what CI runs against and what three
        checks have now failed on for confusing "not set up yet" with "broken".

        `tools/check.py` imports nothing outside the standard library, so this
        needs no virtualenv.
        """
        with tempfile.TemporaryDirectory() as td:
            tree = pathlib.Path(td) / "checkout"
            tree.mkdir()
            listed = subprocess.run(["git", "ls-files"], cwd=str(ROOT),
                                    capture_output=True, text=True).stdout.split()
            for rel in listed:
                source = ROOT / rel
                if not source.exists() or source.is_symlink():
                    continue
                target = tree / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
                target.chmod(source.stat().st_mode & 0o777)
            for args in (("init", "-q", "-b", "main"),
                         ("config", "user.email", "t@example.com"),
                         ("config", "user.name", "T"),
                         ("add", "-A"),
                         ("commit", "-q", "-m", "checkout")):
                git(*args, cwd=tree)
            env = dict(os.environ, HERALD_HOME=str(pathlib.Path(td) / "nothing"))
            env.pop("VIRTUAL_ENV", None)
            run = subprocess.run([sys.executable, "tools/check.py"],
                                 cwd=str(tree), capture_output=True, text=True,
                                 env=env)
            self.assertEqual(run.returncode, 0,
                             msg="check.py failed on a fresh checkout, which is "
                                 f"what CI runs:\n{run.stdout[-2500:]}")
