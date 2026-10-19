"""Installing, updating and vetting an extension that lives in its own repo.

What these defend: somebody else's code arrives switched off, so its first run
is never the timer's; an update fast-forwards or refuses and never merges a
local edit in; and the publish check finds this install's person in a file
before the file goes public.
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

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
       "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"]


def _git(cwd, *args):
    subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True)


def _upstream(where: pathlib.Path, manifest: dict | None) -> pathlib.Path:
    """A repository standing in for somebody's published extension."""
    where.mkdir(parents=True)
    _git(where, "init", "-q")
    if manifest is not None:
        (where / "herald-extension.json").write_text(json.dumps(manifest))
    (where / "README.md").write_text("# demo\n")
    (where / "collectors").mkdir()
    (where / "collectors" / "demo.py").write_text("CADENCE_MINUTES = 60\n")
    _git(where, "add", "-A")
    _git(where, "commit", "-q", "-m", "first")
    return where


class Sharing(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.td.name)
        self.home = base / "home"
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps(
            {"user": {"name": "Ada Lovelace"}}))
        _git(self.home, "init", "-q")
        self.base = base
        self.env = mock.patch.dict(os.environ, {"HERALD_HOME": str(self.home)})
        self.env.start()
        import herald.config as cfg
        import herald.extensions as ext
        import herald.leaks as leaks
        self.cfg = importlib.reload(cfg)
        self.ext = importlib.reload(ext)
        importlib.reload(leaks)
        # sync() writes into the program's checkout; these tests are about the
        # clone, so it is stubbed rather than allowed to touch the real tree.
        self.sync = mock.patch.object(self.ext, "sync", return_value={})
        self.sync.start()

    def tearDown(self):
        self.sync.stop()
        self.env.stop()
        import herald.config as cfg
        import herald.extensions as ext
        importlib.reload(cfg)
        importlib.reload(ext)
        self.td.cleanup()

    def test_installed_extension_arrives_off_and_remembers_its_source(self):
        src = _upstream(self.base / "up", {"name": "demo", "description": "d",
                                           "version": "1", "enabled": True})
        e = self.ext.install(str(src))
        self.assertEqual(e.name, "demo")
        self.assertFalse(e.enabled)
        self.assertEqual(self.cfg.get("extensions.demo.source"), str(src))
        self.assertIn("/extensions/demo/",
                      (self.home / ".gitignore").read_text().splitlines())
        self.assertNotIn("demo", self.ext.collectors())

    def test_a_repo_without_a_manifest_leaves_nothing_behind(self):
        src = _upstream(self.base / "up", None)
        with self.assertRaises(RuntimeError):
            self.ext.install(str(src))
        self.assertEqual(list((self.home / "extensions").iterdir()), [])

    def test_update_fast_forwards_and_refuses_a_local_edit(self):
        src = _upstream(self.base / "up", {"name": "demo", "description": "d",
                                           "version": "1"})
        e = self.ext.install(str(src))
        self.assertEqual(self.ext.update("demo")["status"], "current")

        (src / "collectors" / "demo.py").write_text("CADENCE_MINUTES = 30\n")
        _git(src, "commit", "-q", "-am", "faster")
        r = self.ext.update("demo")
        self.assertEqual(r["status"], "updated")
        self.assertEqual(len(r["log"]), 1)

        (e.path / "collectors" / "demo.py").write_text("SIZE = 'M'\n")
        self.assertEqual(self.ext.update("demo")["status"], "changed")

    def test_publish_check_finds_the_person(self):
        src = _upstream(self.base / "up", {"name": "demo", "description": "d",
                                           "version": "1"})
        e = self.ext.install(str(src))
        problems, _ = self.ext.publish_problems("demo")
        self.assertEqual(problems, [])
        (e.path / "skill.md").write_text("Find clothes for Ada in her sizes.\n")
        _git(e.path, "add", "skill.md")
        problems, _ = self.ext.publish_problems("demo")
        self.assertTrue(any("skill.md:1" in p and "name" in p for p in problems),
                        problems)

    def test_settings_needed_reads_the_manifest(self):
        src = _upstream(self.base / "up", {
            "name": "demo", "description": "d", "version": "1",
            "provides_capabilities": [{"key": "demo", "settings": ["demo.sizes"],
                                       "secrets": ["demo.token"]}]})
        e = self.ext.install(str(src))
        self.assertEqual(e.settings_needed(),
                         [("demo.sizes", "setting"), ("demo.token", "secret")])
        self.cfg.set_user("demo.sizes", {"tops": "M"})
        self.assertEqual(self.ext.find("demo").settings_needed(),
                         [("demo.token", "secret")])


if __name__ == "__main__":
    unittest.main()
