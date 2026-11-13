"""browse_agent: snapshot compaction, step parsing and approval-bound clicks."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

from herald import browse_agent as ba


class Compact(unittest.TestCase):
    def test_containers_and_urls_go_and_refs_stay(self):
        tree = ("- generic [ref=e2]:\n  - link \"Jobs\" [ref=e3] [cursor=pointer]:\n"
                "    - /url: https://x/jobs\n  - button \"Save\" [ref=e4]")
        out = ba.compact(tree)
        self.assertNotIn("generic", out)
        self.assertNotIn("/url", out)
        self.assertIn('button "Save" [ref=e4]', out)

    def test_long_option_lists_collapse(self):
        tree = "- combobox \"Lang\" [ref=e1]:\n" + "".join(f"  - option \"L{i}\"\n" for i in range(30))
        out = ba.compact(tree)
        self.assertIn("22 more options", out)
        self.assertEqual(out.count("- option"), 8)

    def test_protected_values_are_redacted(self):
        out = ba.compact('- textbox "Social Security number" [ref=e5]: 123-45-6789')
        self.assertNotIn("6789", out)


class Parse(unittest.TestCase):
    def test_steps_split_lines_and_strip_at(self):
        steps = ba.parse(['click @e7', 'fill e4 "Alex Kim"\n# note\npick e9 UMD "UMD Loop"'])
        self.assertEqual(steps, [["click", "e7"], ["fill", "e4", "Alex Kim"],
                                 ["pick", "e9", "UMD", "UMD Loop"]])

    def test_unknown_verbs_are_refused(self):
        with self.assertRaises(ValueError):
            ba.parse(["submit e1"])


class ApprovedClicks(unittest.TestCase):
    def test_label_words_must_be_in_the_quote(self):
        a = {"covers": "REMOVE: project \"Coding a Neural Network\"; delete project", "used": None}
        self.assertTrue(ba.approval_covers(a, "Delete project"))
        self.assertFalse(ba.approval_covers(a, "Delete account"))

    def test_one_approval_one_click(self):
        a = {"covers": "delete project", "used": "Delete project"}
        self.assertFalse(ba.approval_covers(a, "Delete project"))

    def test_applications_are_never_covered(self):
        a = {"covers": "submit application", "used": None}
        self.assertFalse(ba.approval_covers(a, "Submit application"))

    def _row(self, **kw):
        row = {"approved": True, "answered_at": time.time() - 60, "approval_id": 9,
               "question": "Apply these edits. REMOVE: the project \"Old thing\"."}
        row.update(kw)
        return row

    def test_quote_must_be_exact_and_fresh(self):
        with mock.patch("herald.questions.result", return_value=self._row()):
            self.assertEqual(ba.approval_for(1, 'REMOVE: the project "Old thing"')["approval_id"], 9)
            with self.assertRaises(SystemExit):
                ba.approval_for(1, "REMOVE: everything")
        with mock.patch("herald.questions.result", return_value=self._row(approved=False)):
            with self.assertRaises(SystemExit):
                ba.approval_for(1, "REMOVE")
        with mock.patch("herald.questions.result",
                        return_value=self._row(answered_at=time.time() - 3 * 3600)):
            with self.assertRaises(SystemExit):
                ba.approval_for(1, "REMOVE")


class LockHolder(unittest.TestCase):
    def test_a_busy_lock_names_its_holder(self):
        from herald import browser_workspaces as bw
        with tempfile.NamedTemporaryFile() as f:
            fd = os.open(f.name, os.O_RDWR)
            with mock.patch.object(sys, "argv", ["herald", "browse", "click", "tab1"]):
                bw.note_holder(fd)
            text = bw.holder(fd)
            os.close(fd)
        self.assertIn(str(os.getpid()), text)
        self.assertIn("browse click tab1", text)


if __name__ == "__main__":
    unittest.main()
