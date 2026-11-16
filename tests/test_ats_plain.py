"""Answers stored as option patterns typed into free-text boxes."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'lib'))
from herald.ats import plain


class Plain(unittest.TestCase):
    def test_grouped_alternation_is_not_split_mid_group(self):
        self.assertEqual(plain('careers? (site|page|website)|(company|corporate) website'), 'careers site')
        self.assertEqual(plain('Company careers site|careers? (site|page)'), 'Company careers site')

    def test_wildcards_and_anchors(self):
        self.assertEqual(plain('Example University.*North Campus|Example U'), 'Example University, North Campus')
        self.assertEqual(plain('^No'), 'No')
        self.assertEqual(plain('Springfield'), 'Springfield')


if __name__ == '__main__':
    unittest.main()
