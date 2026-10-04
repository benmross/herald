"""`herald find` must never return a path that is gone, and must find what
the index cannot know about when told where to look, all without a real
locate database."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import find  # noqa: E402


def fake_locate(paths, returncode=0, stderr=b""):
    """A stand-in for subprocess.run that answers like `locate -0`, and
    records the command it was given."""
    calls = []

    def run(cmd, capture_output=True):
        calls.append(cmd)
        out = b"".join(os.fsencode(p) + b"\0" for p in paths)
        return subprocess.CompletedProcess(cmd, returncode, out, stderr)

    run.calls = calls
    return run


class FindTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name).resolve()
        (self.root / "photos").mkdir()
        (self.root / "photos" / "beach.jpg").write_text("x")

    def tearDown(self):
        self.tmp.cleanup()

    def test_paths_that_are_gone_are_left_out(self):
        here = str(self.root / "photos" / "beach.jpg")
        gone = str(self.root / "photos" / "deleted.jpg")
        a = find.find(find.Query(patterns=["jpg"]), run=fake_locate([here, gone]))
        self.assertEqual(a.paths, [here])
        self.assertEqual(a.dropped, 1)

    def test_live_search_finds_what_the_index_cannot_know(self):
        new = self.root / "photos" / "new-today.jpg"
        new.write_text("y")
        q = find.Query(patterns=["new-today"], fresh=[self.root])
        a = find.find(q, run=fake_locate([], returncode=1))
        self.assertEqual(a.paths, [str(new)])
        self.assertEqual(a.fresh_only, 1)

    def test_live_search_does_not_duplicate_indexed_paths(self):
        here = str(self.root / "photos" / "beach.jpg")
        q = find.Query(patterns=["beach"], fresh=[self.root])
        a = find.find(q, run=fake_locate([here]))
        self.assertEqual(a.paths, [here])
        self.assertEqual(a.fresh_only, 0)

    def test_matching_follows_locate_rules(self):
        p = str(self.root / "photos" / "beach.jpg")
        self.assertTrue(find._matcher(find.Query(patterns=["photos", "beach"]))(p))
        self.assertFalse(find._matcher(find.Query(patterns=["photos"], basename=True))(p))
        self.assertTrue(find._matcher(find.Query(patterns=["BEACH"], ignore_case=True))(p))
        self.assertTrue(find._matcher(find.Query(patterns=[r"b\w+\.jpg$"], regex=True))(p))

    def test_flags_reach_locate_and_limit_leaves_room_for_drops(self):
        run = fake_locate([])
        find.locate(find.Query(patterns=["x"], ignore_case=True, regex=True, basename=True, limit=5), run)
        cmd = run.calls[0]
        for flag in ("-0", "-A", "-i", "-r", "-b"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("-l") + 1], "60")
        self.assertEqual(cmd[-2:], ["--", "x"])

    def test_nothing_found_is_an_answer_but_a_locate_error_is_not(self):
        self.assertEqual(find.locate(find.Query(patterns=["x"]), fake_locate([], returncode=1)), [])
        with self.assertRaises(find.FindError) as cm:
            find.locate(find.Query(patterns=["x"]),
                        fake_locate([], returncode=1, stderr=b"/var/lib/plocate/plocate.db: Permission denied"))
        self.assertIn("plocate group", str(cm.exception))

    def test_index_age_comes_from_the_database_mtime(self):
        db = self.root / "fake.db"
        db.write_text("")
        os.utime(db, (1000, 1000))
        self.assertEqual(find.index_age(db, now=4600), 3600)
        self.assertIsNone(find.index_age(self.root / "missing.db"))
        self.assertEqual(find.describe_age(3600 * 5), "an index 5 h old")


if __name__ == "__main__":
    unittest.main()
