"""The text a person would read, out of a Gmail `format=full` payload.

Kept apart from the collector so it can be tested without Google's client
library, and because it is the part that breaks quietly: a newsletter whose
plain-text part says only "view this in your browser" is not a parse error, it
is a body with nothing in it.
"""

from __future__ import annotations

import base64
import re
from html.parser import HTMLParser

# A bound on storage, not on what a session reads -- `herald fact` and
# `herald db` handle that. Real correspondence never comes near it; the few
# messages that do are pasted logs or generated reports, and those say so in
# `data.body_truncated` rather than silently ending.
MAX_CHARS = 200_000

_BLOCK = {"p", "div", "br", "li", "tr", "table", "section", "article", "header",
          "footer", "blockquote", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "ul",
          "ol"}
_SKIP = {"script", "style", "head", "title"}
# Marketing mail pads its preview text with invisible characters so the inbox
# snippet looks tidy; left in, they are hundreds of characters of nothing.
_INVISIBLE = re.compile(r"[\u034f\u200b\u200c\u200d\u2060\ufeff\u00ad]+")


def _visible(text: str) -> str:
    return _INVISIBLE.sub("", text).replace("\u00a0", " ")


def _decode(data: str) -> str:
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return raw.decode("utf-8", errors="replace")


def _parts(part: dict):
    yield part
    for child in part.get("parts") or []:
        yield from _parts(child)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0
        self._href: str | None = None
        self._anchor: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        elif tag in _BLOCK:
            self.out.append("\n")
        if tag == "li":
            self.out.append("- ")
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._anchor = []

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK:
            self.out.append("\n")
        if tag == "a" and self._href:
            # The link is often the whole point -- the announcement, the form,
            # the room booking -- so it survives, unless the text already is it.
            label = "".join(self._anchor).strip()
            if self._href.startswith("http") and self._href != label:
                self.out.append(f" <{self._href}>")
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        self.out.append(data)
        if self._href:
            self._anchor.append(data)


def html_to_text(markup: str) -> str:
    parser = _Text()
    parser.feed(markup)
    parser.close()
    text = _visible("".join(parser.out))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _clean_plain(text: str) -> str:
    text = _visible(text.replace("\r\n", "\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def body(payload: dict) -> tuple[str, str]:
    """(text, format): format is 'text', 'html' or 'none'."""
    plain, markup = [], []
    for part in _parts(payload or {}):
        if part.get("filename"):
            continue                      # an attached .txt is not the body
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        mime = (part.get("mimeType") or "").lower()
        if mime == "text/plain":
            plain.append(_clean_plain(_decode(data)))
        elif mime == "text/html":
            markup.append(html_to_text(_decode(data)))
    text_plain = "\n\n".join(p for p in plain if p)
    text_html = "\n\n".join(h for h in markup if h)
    # Prefer the plain part, except when it is the stub some senders put there
    # ("view this email in your browser") in front of the real HTML.
    if text_plain and not (len(text_plain) < 200 and len(text_html) > 3 * len(text_plain)):
        return text_plain, "text"
    if text_html:
        return text_html, "html"
    return "", "none"


def attachments(payload: dict) -> list[dict]:
    """What is attached, and the id that fetches each one from Gmail."""
    found = []
    for part in _parts(payload or {}):
        if part.get("filename"):
            b = part.get("body") or {}
            found.append({"filename": part["filename"],
                          "mime_type": part.get("mimeType"),
                          "size": b.get("size"),
                          "attachment_id": b.get("attachmentId")})
    return found
