"""Core reaches extensions through their manifests, never by name.

Before this, core listed 'purelymail', 'canvas', 'textview' and 'discord' in
its queries, and `herald cf` lived in core. These pin the replacements: a
manifest's `sources` and `commands`, the published catalog, and the home
region that is no longer one city's by default.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import extensions  # noqa: E402


def _ext(base: pathlib.Path, name: str, manifest: dict, lib: dict | None = None):
    path = base / name
    (path / "lib").mkdir(parents=True)
    for module, source in (lib or {}).items():
        (path / "lib" / f"{module}.py").write_text(source)
    return extensions.Extension(name=name, path=path, manifest={"name": name, **manifest})


class Sources(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.base = pathlib.Path(self.td.name)

    def _enabled(self, *exts):
        return mock.patch.object(extensions, "enabled", return_value=list(exts))

    def test_mail_role_joins_gmail_and_nothing_else_does(self):
        mail = _ext(self.base, "fastmail", {"sources": {"fastmail": {"role": "mail"}}})
        chat = _ext(self.base, "chatty", {"sources": {"chatty": {"search": {"message": "chat"}}}})
        with self._enabled(mail, chat):
            self.assertEqual(extensions.mail_sources(), ("gmail", "fastmail"))
            self.assertEqual(extensions.search_labels(), {("chatty", "message"): "chat"})

    def test_without_extensions_only_gmail_is_mail(self):
        with self._enabled():
            self.assertEqual(extensions.mail_sources(), ("gmail",))
            self.assertEqual(extensions.sql_list(extensions.mail_sources()), "('gmail')")

    def test_a_source_name_that_could_break_sql_is_dropped(self):
        self.assertEqual(extensions.sql_list(["gmail", "x'); DROP TABLE facts;--"]), "('gmail')")
        self.assertEqual(extensions.sql_list([]), "('')")

    def test_old_manifests_keep_their_search_labels(self):
        old = _ext(self.base, "canvas", {})
        with self._enabled(old):
            self.assertEqual(extensions.search_labels()[("canvas", "assignment")], "course")

    def test_source_function_loads_from_the_extensions_own_lib(self):
        mail = _ext(self.base, "fastmail",
                    {"sources": {"fastmail": {"role": "mail", "images": "fm_images_test:images"}}},
                    {"fm_images_test": "def images(*a):\n    return ['saved'], {}\n"})
        with self._enabled(mail), mock.patch.object(extensions, "find", return_value=mail):
            self.assertEqual(extensions.source_function("fastmail", "images")()[0], ["saved"])
            self.assertIsNone(extensions.source_function("gmail", "images"))


class Commands(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.base = pathlib.Path(self.td.name)

    def test_a_command_runs_with_its_arguments(self):
        ext = _ext(self.base, "tides", {"commands": {"tides": "tides_cmd_test:main"}},
                   {"tides_cmd_test": "def main(argv):\n    return len(argv)\n"})
        with mock.patch.object(extensions, "enabled", return_value=[ext]):
            self.assertEqual(extensions.run_command("tides", ["a", "b"]), 2)
            self.assertIsNone(extensions.run_command("nothing", []))

    def test_a_name_two_extensions_claim_goes_to_neither(self):
        a = _ext(self.base, "a", {"commands": {"go": "x:main"}})
        b = _ext(self.base, "b", {"commands": {"go": "y:main"}})
        with mock.patch.object(extensions, "enabled", return_value=[a, b]):
            self.assertNotIn("go", extensions.commands())

    def test_cloudflare_ships_as_a_bundled_command(self):
        manifest = json.loads((ROOT / "extensions/cloudflare/herald-extension.json").read_text())
        self.assertEqual(manifest["commands"], {"cf": "cloudflare:main"})
        self.assertFalse(manifest["enabled"])
        self.assertFalse((ROOT / "lib/herald/cloudflare.py").exists())


class Catalog(unittest.TestCase):
    def test_every_entry_has_what_setup_shows(self):
        listed = json.loads(extensions.CATALOG_PATH.read_text())["extensions"]
        self.assertTrue(listed)
        for item in listed:
            for key in ("name", "url", "title", "for", "summary"):
                self.assertTrue(item.get(key), (item, key))
            self.assertTrue(item["url"].startswith("https://"))


class HomeRegion(unittest.TestCase):
    def _internships(self):
        spec = importlib.util.spec_from_file_location("internships_t", ROOT / "collectors/internships.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_unset_means_nothing_is_near_home(self):
        mod = self._internships()
        with mock.patch("herald.config.get", return_value=None):
            pattern, label = mod.home_region()
        self.assertIsNone(pattern)
        row = {"active": True, "locations": ["Bethesda, MD"], "title": "Intern"}
        self.assertEqual(mod._relevant(row, set(), (pattern, label)), (False, []))

    def test_a_configured_region_admits_and_labels(self):
        mod = self._internships()
        values = {"jobs.home_region": mod.DMV_PATTERN, "jobs.home_region_label": "DMV"}
        with mock.patch("herald.config.get", side_effect=lambda k, d=None: values.get(k, d)):
            home = mod.home_region()
        row = {"active": True, "locations": ["Bethesda, MD"], "title": "Intern"}
        self.assertEqual(mod._relevant(row, set(), home), (True, ["DMV"]))
        self.assertEqual(mod._relevant({**row, "locations": ["Columbia, SC"]}, set(), home), (False, []))


if __name__ == "__main__":
    unittest.main()
