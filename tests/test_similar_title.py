"""gwrite.similar_title: when two event titles name the same event."""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))
from herald import gwrite  # noqa: E402


class SimilarTitle(unittest.TestCase):
    def test_extra_word_matches(self):
        self.assertTrue(gwrite.similar_title("Cipher Tech Lobby Day",
                                             "Cipher Tech Solutions Lobby Day"))

    def test_word_forms_match(self):
        self.assertTrue(gwrite.similar_title("Terp Traditions Month Table",
                                             "Terp Traditions Month Tabling"))

    def test_one_word_never_matches(self):
        self.assertFalse(gwrite.similar_title("Tabling", "Tabling Fundraiser"))

    def test_different_events_do_not_match(self):
        self.assertFalse(gwrite.similar_title("Lindy Hop Lessons (Beginner)",
                                              "Salsa Lessons (Beginner)"))


if __name__ == "__main__":
    unittest.main()
