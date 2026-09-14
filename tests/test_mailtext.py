"""Mail bodies: the text a person would read, not a parse that merely succeeded."""

from __future__ import annotations

import base64
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import mailtext  # noqa: E402


def _part(mime: str, text: str, filename: str = "") -> dict:
    data = base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
    return {"mimeType": mime, "filename": filename, "body": {"data": data, "size": len(text)}}


class Body(unittest.TestCase):
    def test_plain_is_preferred(self):
        payload = {"mimeType": "multipart/alternative", "parts": [
            _part("text/plain", "There will be no lab on Tuesday."),
            _part("text/html", "<p>There will be no lab on Tuesday.</p>")]}
        self.assertEqual(mailtext.body(payload),
                         ("There will be no lab on Tuesday.", "text"))

    def test_html_is_converted_with_links_kept(self):
        html = ("<html><head><style>p{color:red}</style></head><body>"
                "<p>Read the <a href='https://example.edu/a/1'>announcement</a>.</p>"
                "<script>alert(1)</script><ul><li>one</li><li>two</li></ul></body></html>")
        text, fmt = mailtext.body(_part("text/html", html))
        self.assertEqual(fmt, "html")
        self.assertIn("announcement <https://example.edu/a/1>", text)
        self.assertIn("- one", text)
        self.assertNotIn("color", text)
        self.assertNotIn("alert", text)

    def test_a_view_in_browser_stub_loses_to_the_real_html(self):
        real = "<p>" + "Office hours move to Thursday this week. " * 20 + "</p>"
        payload = {"mimeType": "multipart/alternative", "parts": [
            _part("text/plain", "View this email in your browser."),
            _part("text/html", real)]}
        text, fmt = mailtext.body(payload)
        self.assertEqual(fmt, "html")
        self.assertIn("Office hours move to Thursday", text)

    def test_invisible_preheader_padding_is_removed(self):
        text, _ = mailtext.body(_part("text/plain", "Hi‌͏‌͏ there"))
        self.assertEqual(text, "Hi there")

    def test_an_attached_text_file_is_not_the_body(self):
        payload = {"mimeType": "multipart/mixed", "parts": [
            _part("text/plain", "See attached."),
            _part("text/plain", "secret file contents", filename="notes.txt")]}
        self.assertEqual(mailtext.body(payload)[0], "See attached.")
        self.assertEqual([a["filename"] for a in mailtext.attachments(payload)],
                         ["notes.txt"])

    def test_nothing_readable(self):
        self.assertEqual(mailtext.body({"mimeType": "multipart/mixed"}), ("", "none"))


if __name__ == "__main__":
    unittest.main()
