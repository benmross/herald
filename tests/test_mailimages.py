"""An unattended job that fetches URLs out of mail has to refuse most of them.

Scout reads every message whole, pictures included, since 13 October 2026. A
linked image is an address chosen by whoever wrote the mail, requested from
inside the user's own network. These tests are the ways that could go wrong:
a request to a LAN service, a redirect to one, and tracking pixels or icons
being handed to a session as though they were content.
"""

from __future__ import annotations

import base64
import io
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import mailimages, mailtext  # noqa: E402

try:
    from PIL import Image
except ImportError:                                        # pragma: no cover
    Image = None


def _png(w: int, h: int, colour=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, "PNG")
    return buf.getvalue()


class Addresses(unittest.TestCase):
    def test_private_and_local_addresses_refused(self):
        for url in ("http://127.0.0.1/a.png", "http://10.0.0.5/a.png",
                    "http://192.168.1.1/a.png", "http://100.101.102.103/a.png",
                    "http://169.254.169.254/latest/meta-data", "http://[::1]/a.png",
                    "http://localhost/a.png"):
            self.assertFalse(mailimages.allowed_url(url), url)

    def test_only_web_schemes_and_ports(self):
        for url in ("file:///etc/passwd", "ftp://8.8.8.8/a.png",
                    "http://8.8.8.8:8787/a.png", "http://user:pw@8.8.8.8/a.png",
                    "not a url"):
            self.assertFalse(mailimages.allowed_url(url), url)
        self.assertTrue(mailimages.allowed_url("https://8.8.8.8/a.png"))


class Sizes(unittest.TestCase):
    def test_pixels_icons_and_dividers_dropped(self):
        self.assertFalse(mailimages.worth_reading(1, 1))
        self.assertFalse(mailimages.worth_reading(48, 48))
        self.assertFalse(mailimages.worth_reading(1200, 20))
        self.assertTrue(mailimages.worth_reading(700, 350))
        self.assertTrue(mailimages.worth_reading(1080, 1080))


@unittest.skipIf(Image is None, "Pillow not installed")
class Collect(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.seen = mailimages.Seen(self.tmp / "seen.json")

    def _part(self, raw: bytes) -> dict:
        return {"kind": "part", "mime_type": "image/png", "filename": "flyer.png",
                "data": base64.urlsafe_b64encode(raw).decode(), "attachment_id": None}

    def test_inline_flyer_saved_and_icon_skipped(self):
        found = [self._part(_png(800, 600)), self._part(_png(32, 32))]
        saved, skipped = mailimages.collect(found, self.tmp / "m1", seen=self.seen,
                                            remote=False, limit=6)
        self.assertEqual(len(saved), 1)
        self.assertEqual(skipped, {"small": 1})
        self.assertTrue(pathlib.Path(saved[0]["path"]).is_file())

    def test_same_banner_is_read_once(self):
        found = [self._part(_png(800, 200))]
        first, _ = mailimages.collect(found, self.tmp / "m1", seen=self.seen,
                                      remote=False, limit=6)
        again, skipped = mailimages.collect(found, self.tmp / "m2", seen=self.seen,
                                            remote=False, limit=6)
        self.assertEqual((len(first), len(again)), (1, 0))
        self.assertEqual(skipped, {"seen": 1})

    def test_linked_images_wait_for_the_setting(self):
        found = [{"kind": "remote", "url": "https://8.8.8.8/flyer.png", "alt": "",
                  "width": 600, "height": 400},
                 {"kind": "remote", "url": "https://8.8.8.8/open/pixel.gif", "alt": "",
                  "width": 1, "height": 1}]
        saved, skipped = mailimages.collect(found, self.tmp / "m1", seen=self.seen,
                                            remote=False, limit=6)
        self.assertEqual(saved, [])
        self.assertEqual(skipped, {"small": 1, "remote_off": 1})

    def test_limit_holds(self):
        found = [self._part(_png(400 + i, 400)) for i in range(5)]
        saved, skipped = mailimages.collect(found, self.tmp / "m1", seen=self.seen,
                                            remote=False, limit=2)
        self.assertEqual(len(saved), 2)
        self.assertEqual(skipped, {"over_limit": 3})


class FromPayload(unittest.TestCase):
    def test_images_found_in_parts_and_html(self):
        html = ('<p>Hi</p><img src="https://cdn.example.com/flyer.png" width="600" '
                'alt="Talk, Oct 21"><img src="cid:logo">')
        payload = {"mimeType": "multipart/related", "parts": [
            {"mimeType": "text/html",
             "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()}},
            {"mimeType": "image/png", "filename": "logo.png",
             "body": {"attachmentId": "att-1", "size": 9000}}]}
        found = mailtext.images(payload)
        self.assertEqual([f["kind"] for f in found], ["remote", "part"])
        self.assertEqual(found[0]["alt"], "Talk, Oct 21")
        self.assertEqual(found[0]["width"], 600)
        self.assertEqual(found[1]["attachment_id"], "att-1")


class Compact(unittest.TestCase):
    def test_stylesheet_padding_and_tracking_tokens_removed(self):
        text = ("table.button td {\n  background: #fa423b;\n  color: white;\n}\n"
                "a { color: red; }\n\nSam,\nThe fair is this week. ‍͏ ‍͏\n"
                "----------------------\n"
                "Apply: https://example.com/apply?id=7 or https://t.example.com/c/"
                + "A" * 300 + "\nfont-size: 12px; color: red;\nThanks")
        out = mailtext.compact(text)
        self.assertNotIn("background", out)
        self.assertNotIn("font-size", out)
        self.assertNotIn("‍", out)
        self.assertNotIn("----", out)
        self.assertIn("https://example.com/apply?id=7", out)
        self.assertIn("https://t.example.com/…", out)
        self.assertNotIn("AAAA", out)
        self.assertTrue(out.startswith("Sam,"))

    def test_ordinary_prose_is_left_alone(self):
        text = "Hi Sam,\n\nCan you send the form by Friday? Cost: $40; room: 2108.\n\nThanks"
        self.assertEqual(mailtext.compact(text), text)

    def test_alt_text_survives_html(self):
        self.assertIn("[image: Flyer: Oct 21, 6pm]",
                      mailtext.html_to_text('<p>See <img src="x" alt="Flyer: Oct 21, 6pm"></p>'))


if __name__ == "__main__":
    unittest.main()
