"""Capability gating: off is not the same as broken.

The distinction these tests defend is the one a second user immediately runs
into. Herald ran thirteen collectors because its first user had connected
thirteen sources; somebody who connected two should see two, and should not be
told every half hour that the other eleven are failing at credentials they were
never asked for.
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


def _fresh(home: pathlib.Path, config: dict, secrets: dict | None = None):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps(config))
    if secrets is not None:
        (home / "secrets.json").write_text(json.dumps(secrets))
    with mock.patch.dict(os.environ, {"HERALD_HOME": str(home)}):
        import herald.config as cfg
        import herald.capabilities as caps
        importlib.reload(cfg)
        return importlib.reload(caps)


class Gating(unittest.TestCase):
    def tearDown(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERALD_HOME", None)
            import herald.config as cfg
            import herald.capabilities as caps
            importlib.reload(cfg)
            importlib.reload(caps)

    def test_a_capability_nobody_enabled_is_off(self):
        with tempfile.TemporaryDirectory() as td:
            caps = _fresh(pathlib.Path(td), {"capabilities": {}})
            self.assertFalse(caps.available("telegram"))
            self.assertEqual(caps.missing("telegram"), "not enabled")

    def test_enabled_but_uncredentialled_says_which_credential(self):
        """The reason is the whole value of this. 'telegram is off' sends
        someone looking in the wrong place; naming the missing key does not."""
        with tempfile.TemporaryDirectory() as td:
            caps = _fresh(pathlib.Path(td), {"capabilities": {"telegram": True}}, {})
            self.assertIn("telegram.bot_token", caps.missing("telegram"))

    def test_enabled_and_credentialled_is_available(self):
        with tempfile.TemporaryDirectory() as td:
            caps = _fresh(pathlib.Path(td), {"capabilities": {"telegram": True}},
                          {"telegram": {"bot_token": "123:abc"}})
            self.assertIsNone(caps.missing("telegram"))
            self.assertTrue(caps.available("telegram"))

    def test_an_unknown_capability_is_a_typo_not_a_feature(self):
        with tempfile.TemporaryDirectory() as td:
            caps = _fresh(pathlib.Path(td), {"capabilities": {"telegrams": True}})
            self.assertIn("unknown", caps.missing("telegrams"))

    def test_a_settings_backed_capability_needs_its_setting(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td)
            caps = _fresh(home, {"capabilities": {"calendar_feeds": True}})
            self.assertIn("calendar_feeds", caps.missing("calendar_feeds"))
            caps = _fresh(home, {"capabilities": {"calendar_feeds": True},
                                 "calendar_feeds": [{"source": "x", "url": "y"}]})
            self.assertIsNone(caps.missing("calendar_feeds"))

    def test_a_file_backed_capability_checks_the_file(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td)
            token = home / "token.json"
            caps = _fresh(home, {"capabilities": {"google": True},
                                 "google": {"credentials_dir": str(home)}})
            self.assertIn("token.json", caps.missing("google"))
            token.write_text("{}")
            caps = _fresh(home, {"capabilities": {"google": True},
                                 "google": {"credentials_dir": str(home)}})
            self.assertIsNone(caps.missing("google"))

    def test_extensions_declare_capabilities_without_being_imported(self):
        """Asking an extension what it provides must never mean running its
        code: the manifest is read, the Python is not."""
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td)
            ext = home / "extensions" / "boat"
            ext.mkdir(parents=True)
            (ext / "herald-extension.json").write_text(json.dumps({
                "name": "boat",
                "provides_capabilities": [{"key": "tides", "title": "Tides"}],
            }))
            (ext / "collectors").mkdir()
            (ext / "collectors" / "tides.py").write_text("raise SystemExit('imported!')")
            caps = _fresh(home, {"capabilities": {"tides": True}})
            self.assertIn("tides", caps.registry())
            self.assertEqual(caps.registry()["tides"].extension, "boat")
            self.assertTrue(caps.available("tides"))

    def test_a_disabled_extension_provides_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td)
            ext = home / "extensions" / "boat"
            ext.mkdir(parents=True)
            (ext / "herald-extension.json").write_text(json.dumps({
                "name": "boat", "enabled": False,
                "provides_capabilities": [{"key": "tides"}]}))
            caps = _fresh(home, {"capabilities": {"tides": True}})
            self.assertNotIn("tides", caps.registry())

    def test_unmet_reports_every_reason_not_just_the_first(self):
        with tempfile.TemporaryDirectory() as td:
            caps = _fresh(pathlib.Path(td), {"capabilities": {"github": True}}, {})
            unmet = caps.unmet(("github", "telegram", "jobs"))
            self.assertEqual([k for k, _ in unmet], ["telegram", "jobs"])


if __name__ == "__main__":
    unittest.main()
