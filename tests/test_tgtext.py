"""Markdown to Telegram HTML, with the historical failure as the first test.

Every case here is either a shape Herald actually sends or a way somebody
else's text has broken a message before. The one that matters most is
`test_two_asterisks_in_urls_stay_literal`: that exact input silently corrupted
two URLs and cost the project its Markdown rendering for a day.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import tgtext  # noqa: E402


class TheFailureThatRemovedMarkdown(unittest.TestCase):
    def test_two_asterisks_in_urls_stay_literal(self):
        """Telegram's own parser read these as a bold span, accepted the
        message, stripped both asterisks and delivered two wrong URLs. Nothing
        was rejected, so no retry could have caught it."""
        source = ("Two recipes: https://example.edu/menu?RecNumAndPort=119388*1 "
                  "and https://example.edu/menu?RecNumAndPort=204411*1")
        out = tgtext.to_html(source)
        self.assertIn("119388*1", out)
        self.assertIn("204411*1", out)
        self.assertNotIn("<b>", out)

    def test_a_lone_asterisk_is_not_emphasis(self):
        self.assertIn("2 * 3", tgtext.to_html("2 * 3 = 6"))

    def test_an_asterisk_inside_code_is_not_emphasis(self):
        out = tgtext.to_html("run `select * from facts` and `select * from runs`")
        self.assertEqual(out.count("<code>"), 2)
        self.assertNotIn("<i>", out)
        self.assertIn("select * from facts", out)

    def test_underscores_in_an_identifier_are_not_emphasis(self):
        out = tgtext.to_html("the field is user_has_replied and awaiting_reply")
        self.assertNotIn("<i>", out)
        self.assertIn("user_has_replied", out)


class Formatting(unittest.TestCase):
    def test_bold(self):
        self.assertEqual(tgtext.to_html("**now**"), "<b>now</b>")

    def test_italic(self):
        self.assertEqual(tgtext.to_html("*soon*"), "<i>soon</i>")

    def test_heading_becomes_bold(self):
        self.assertEqual(tgtext.to_html("## Today"), "<b>Today</b>")

    def test_bullets_become_bullet_characters(self):
        out = tgtext.to_html("- one\n- two")
        self.assertEqual(out, "• one\n• two")

    def test_a_link_keeps_its_label(self):
        out = tgtext.to_html("[the posting](https://example.com/a?b=1&c=2)")
        self.assertIn('<a href="https://example.com/a?b=1&amp;c=2">', out)
        self.assertIn(">the posting</a>", out)

    def test_a_non_web_link_is_not_a_tag(self):
        """Telegram rejects an href it does not understand, and a rejected
        message is a lost message."""
        out = tgtext.to_html("[the file](file:///etc/passwd)")
        self.assertNotIn("<a", out)
        self.assertIn("the file", out)

    def test_fenced_code_is_preformatted(self):
        """Telegram's HTML mode requires only `<`, `>` and `&` to be escaped in
        text, so a quoted shell command inside <pre> stays copy-pasteable."""
        out = tgtext.to_html("before\n\n```\nherald db \"select 1\"\n```\n\nafter")
        self.assertIn('<pre>herald db "select 1"</pre>', out)

    def test_a_horizontal_rule_does_not_become_emphasis(self):
        self.assertNotIn("<i>", tgtext.to_html("above\n\n---\n\nbelow"))


class NobodyElsesTextCanBecomeMarkup(unittest.TestCase):
    def test_angle_brackets_are_escaped(self):
        out = tgtext.to_html("mail from <someone@example.com> arrived")
        self.assertIn("&lt;someone@example.com&gt;", out)
        self.assertNotIn("<someone", out)

    def test_an_html_tag_in_a_subject_is_inert(self):
        out = tgtext.to_html("Subject: <b>free money</b> <script>x()</script>")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_an_ampersand_survives(self):
        self.assertIn("&amp;", tgtext.to_html("Tom & Jerry"))

    def test_nothing_raises_on_pathological_input(self):
        for source in ("*", "**", "***", "`", "```", "[", "](", "_ _ _",
                       "> ", "#", "~~", "<", "&", "\x00", "a" * 5000):
            tgtext.to_html(source)


class Chunking(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(tgtext.chunks("hello"), ["hello"])

    def test_long_text_splits_on_paragraphs(self):
        source = "\n\n".join(["para " + "x" * 500 for _ in range(20)])
        pieces = tgtext.chunks(source, limit=1200)
        self.assertGreater(len(pieces), 1)
        for piece in pieces:
            self.assertLessEqual(len(piece), 1200)

    def test_every_chunk_converts_to_balanced_html(self):
        """The source is split, not the HTML, precisely so no tag is ever cut
        in half -- Telegram rejects that outright."""
        source = "\n\n".join(f"**bold {i}** and `code {i}` and [a](https://e.com/{i})"
                             for i in range(60))
        for piece in tgtext.chunks(source, limit=900):
            out = tgtext.to_html(piece)
            self.assertEqual(out.count("<b>"), out.count("</b>"))
            self.assertEqual(out.count("<code>"), out.count("</code>"))
            self.assertEqual(out.count("<a "), out.count("</a>"))

    def test_a_single_enormous_paragraph_is_still_cut(self):
        pieces = tgtext.chunks("x" * 5000, limit=1000)
        self.assertTrue(all(len(p) <= 1000 for p in pieces))


class Helpers(unittest.TestCase):
    def test_code_block_escapes_and_wraps(self):
        out = tgtext.code_block("‣ Bash: grep '<x>' file")
        self.assertTrue(out.startswith("<pre>"))
        self.assertIn("&lt;x&gt;", out)

    def test_escape_is_plain(self):
        self.assertEqual(tgtext.escape("a<b>c"), "a&lt;b&gt;c")


if __name__ == "__main__":
    unittest.main()


class OnlyTheEntitiesTelegramDocuments(unittest.TestCase):
    """Telegram's HTML mode documents `&lt; &gt; &amp; &quot;` and nothing
    else. `html.escape` also emits `&#x27;` for an apostrophe, which is the
    most common punctuation in English prose -- so escaping it turns every
    other line into `it&#x27;s` if Telegram does not decode numeric entities.
    """

    def test_an_apostrophe_stays_an_apostrophe(self):
        out = tgtext.to_html("It's the collector's cursor")
        self.assertIn("It's the collector's cursor", out)
        self.assertNotIn("&#x27;", out)

    def test_a_double_quote_in_prose_stays(self):
        out = tgtext.to_html('she said "no" twice')
        self.assertIn('"no"', out)
        self.assertNotIn("&quot;", out)

    def test_a_double_quote_in_a_url_is_escaped(self):
        """Inside an attribute it does have to go, or the tag ends early."""
        out = tgtext.to_html('[x](https://e.com/a?b="c")')
        self.assertIn("&quot;c&quot;", out)

    def test_the_three_that_must_be_escaped_are(self):
        out = tgtext.to_html("a < b & c > d")
        self.assertIn("&lt;", out)
        self.assertIn("&amp;", out)
        self.assertIn("&gt;", out)

    def test_an_apostrophe_inside_code_survives(self):
        out = tgtext.to_html("`select * from x where y='z'`")
        self.assertIn("y='z'", out)
