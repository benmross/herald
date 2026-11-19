"""Content omitted by the Telegram attachment viewer must remain readable.

These fixtures reproduce a diagnostic document viewed on the desktop client:
level three/four headings vanished, task states were stripped, HTML div and
summary content disappeared, and a display equation was entirely missing.
"""

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))
from herald import notify, tgmarkdown  # noqa: E402


class ViewerContent(unittest.TestCase):
    def test_deep_headings_keep_their_words(self):
        out = tgmarkdown.for_viewer("# Title\n## Section\n### Missing A\n#### Missing B\n")
        self.assertEqual(out, "# Title\n## Section\n## Missing A\n## Missing B\n")

    def test_task_states_become_visible_characters(self):
        out = tgmarkdown.for_viewer("- [ ] Pending\n- [x] Done\n  - [X] Nested\n1. [ ] Next\n")
        self.assertEqual(out, "- ☐ Pending\n- ☑ Done\n  - ☑ Nested\n1. ☐ Next\n")

    def test_html_text_and_summary_cannot_be_swallowed_as_html(self):
        source = ('<div>\nHTML A\n<p>HTML B</p>\n</div>\n\n'
                  '<details>\n<summary>Summary</summary>\n\nBody\n\n</details>\n')
        out = tgmarkdown.for_viewer(source)
        for text in ("HTML A", "HTML B", "Summary", "Body"):
            self.assertIn(text, out)
        self.assertNotIn("<div>", out)
        self.assertIn("&lt;summary&gt;Summary&lt;/summary&gt;", out)

    def test_display_math_becomes_readable_source(self):
        for expression in ("$$\ny = x^2 + 3\n$$", "$$y = x^2 + 3$$",
                           "\\[\ny = x^2 + 3\n\\]"):
            with self.subTest(expression=expression):
                out = tgmarkdown.for_viewer("Before\n\n" + expression + "\n\nAfter\n")
                self.assertIn("```latex\ny = x^2 + 3\n```", out)
                self.assertTrue(out.endswith("After\n"))

    def test_working_formats_and_autolinks_are_unchanged(self):
        source = ("**Bold** *italic* ~~strike~~ $x + 1 = 2$\n\n"
                  "| A | B |\n| --- | --- |\n| 1 | 2 |\n\n"
                  "> quote\n\n[link](https://example.com)\n<https://example.com>\n"
                  "<a@example.com>\nx < y & y > z\n")
        self.assertEqual(tgmarkdown.for_viewer(source), source)

    def test_code_examples_are_not_rewritten(self):
        source = ("```markdown\n### Example\n- [ ] Example\n<div>Example</div>\n$$x$$\n```\n"
                  "~~~\n#### Example\n~~~\n"
                  "    ### Indented\n    <div>Indented</div>\n"
                  "`<div>` and `` ` ### Example <p> ``\n"
                  "> ```\n> ### Quoted code\n> ```\n")
        self.assertEqual(tgmarkdown.for_viewer(source), source)

    def test_a_shorter_fence_does_not_end_a_code_example(self):
        source = "````markdown\n```\n### Still code\n```\n````\n### Actual heading\n"
        out = tgmarkdown.for_viewer(source)
        self.assertIn("### Still code", out)
        self.assertTrue(out.endswith("## Actual heading\n"))

    def test_unclosed_code_stays_literal(self):
        source = "```markdown\n### Heading\n<div>text</div>\n"
        self.assertEqual(tgmarkdown.for_viewer(source), source)

    def test_multiline_code_span_stays_literal(self):
        source = "`start\n### Heading\n<div>text</div>\nend`\n"
        self.assertEqual(tgmarkdown.for_viewer(source), source)

    def test_comments_remain_intentionally_hidden(self):
        source = "<!--\n### Private editorial note\n<div>hidden</div>\n-->\n"
        self.assertEqual(tgmarkdown.for_viewer(source), source)

    def test_transform_is_idempotent_and_keeps_unicode(self):
        source = "### café α 中文\n- [ ] Task\n<div>visible</div>\n\n$$x^2$$\n"
        once = tgmarkdown.for_viewer(source)
        self.assertEqual(tgmarkdown.for_viewer(once), once)
        self.assertIn("café α 中文", once)

    def test_invalid_utf8_is_passed_through(self):
        blob = b"### Heading\xff\n"
        self.assertEqual(tgmarkdown.attachment(blob), blob)

    def test_line_endings_in_code_are_unchanged(self):
        source = "```\r\n### code\r\n```\r\n### Heading\r\n"
        out = tgmarkdown.for_viewer(source)
        self.assertEqual(out, "```\r\n### code\r\n```\r\n## Heading\r\n")


class Delivery(unittest.TestCase):
    def test_uploaded_bytes_are_adapted_without_changing_the_file(self):
        original = b"### Missing heading\n- [ ] Pending\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "report.MD"
            path.write_bytes(original)
            with mock.patch.object(notify, "_telegram_chat_id", return_value=123), \
                 mock.patch.object(notify, "_telegram_upload", return_value={"ok": True}) as upload:
                self.assertTrue(notify.telegram_document(path, caption="Report", thread_id=9,
                                                        record=False))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(upload.call_args.args[:3], ("sendDocument", "document", "report.MD"))
            self.assertIn(b"## Missing heading\n", upload.call_args.args[3])
            self.assertIn("☐".encode(), upload.call_args.args[3])
            self.assertEqual(upload.call_args.kwargs["message_thread_id"], 9)

    def test_other_files_and_verbatim_transfers_keep_exact_bytes(self):
        original = b"### Keep\n- [ ] Exact\n<div>source</div>\n"
        with tempfile.TemporaryDirectory() as tmp:
            for name, verbatim in (("report.pdf", False), ("report.md", True),
                                   ("report.markdown", True)):
                with self.subTest(name=name, verbatim=verbatim):
                    path = pathlib.Path(tmp) / name
                    path.write_bytes(original)
                    with mock.patch.object(notify, "_telegram_chat_id", return_value=123), \
                         mock.patch.object(notify, "_telegram_upload", return_value={"ok": True}) as upload:
                        notify.telegram_document(path, thread_id=9, record=False, verbatim=verbatim)
                    self.assertEqual(upload.call_args.args[3], original)


if __name__ == "__main__":
    unittest.main()
