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
_INVISIBLE = re.compile(
    r"[\u034f\u061c\u115f\u1160\u17b4\u17b5\u180e\u200b-\u200f\u202a-\u202e"
    r"\u2060-\u2064\u2066-\u206f\u2800\u3164\ufe00-\ufe0f\ufeff\uffa0\u00ad]+")


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
        if tag == "img" and not self._skip:
            alt = (dict(attrs).get("alt") or "").strip()
            if len(alt) > 2:
                self.out.append(f" [image: {alt}] ")

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


# --------------------------------------------------------------------------
# For a model reading the whole message
# --------------------------------------------------------------------------

# A plain-text part is sometimes the HTML's stylesheet pasted in front of the
# words: `table.button table td {`, three declarations, `}`. None of it is
# content, and a mail that opens with forty lines of it buries what it says.
_CSS_BLOCK = re.compile(r"(?ms)^[^\n{}]{1,200}\{[^{}]{0,2000}\}[ \t]*$")
_CSS_LINE = re.compile(
    r"(?m)^\s*(?:[a-z-]{2,30}\s*:\s*[^;\n]{1,120};\s*)+$|^\s*@(?:media|import|font-face)[^\n]*$")
_URL = re.compile(r"https?://[^\s<>()\[\]\"']+")
_RULE = re.compile(r"(?m)^[ \t]*[-=_*~\u2500-\u257f]{4,}[ \t]*$")
URL_KEEP = 140


def _short_url(match: re.Match) -> str:
    url = match.group(0)
    if len(url) <= URL_KEEP:
        return url
    # A redirect through a tracking host: three hundred characters of opaque
    # token. The host says where it goes; the whole link is still on the fact.
    host = url.split("/", 3)[2]
    return f"https://{host}/…"


def compact(text: str) -> str:
    """The words of a message without what only a renderer needs.

    Stored bodies stay whole. This is what a cycle puts in front of a model
    when it hands it every message in full: stylesheets, invisible padding,
    ruled lines and tracking tokens removed, so that reading everything costs
    what the content costs.
    """
    text = _visible(text or "")
    for _ in range(3):                       # nested and adjacent rule blocks
        text, n = _CSS_BLOCK.subn("", text)
        if not n:
            break
    text = _CSS_LINE.sub("", text)
    text = _RULE.sub("", text)
    text = _URL.sub(_short_url, text)
    text = re.sub(r"<(https://[^\s>]+/…)>", r"\1", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class _Imgs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found: list[dict] = []

    def handle_starttag(self, tag, attrs):
        if tag != "img":
            return
        a = dict(attrs)
        src = (a.get("src") or "").strip()
        if not src:
            return

        def _px(v):
            m = re.match(r"\s*(\d+)", v or "")
            return int(m.group(1)) if m else None
        self.found.append({"src": src, "alt": (a.get("alt") or "").strip(),
                           "width": _px(a.get("width")), "height": _px(a.get("height"))})


def images(payload: dict) -> list[dict]:
    """Every picture a message carries, attached, inline or linked.

    Each entry is one of:
      {"kind": "part", "mime_type", "filename", "data" | "attachment_id", "size"}
      {"kind": "remote", "url", "alt", "width", "height"}
    Nothing is fetched here. `cid:` references resolve to parts that are
    already listed, so they are dropped and not counted twice.
    """
    found: list[dict] = []
    for part in _parts(payload or {}):
        mime = (part.get("mimeType") or "").lower()
        b = part.get("body") or {}
        if mime.startswith("image/"):
            found.append({"kind": "part", "mime_type": mime,
                          "filename": part.get("filename") or "",
                          "data": b.get("data"), "attachment_id": b.get("attachmentId"),
                          "size": b.get("size")})
        elif mime == "text/html" and b.get("data") and not part.get("filename"):
            parser = _Imgs()
            parser.feed(_decode(b["data"]))
            parser.close()
            for img in parser.found:
                if img["src"].lower().startswith(("http://", "https://")):
                    found.append({"kind": "remote", "url": img["src"], "alt": img["alt"],
                                  "width": img["width"], "height": img["height"]})
    return found
