"""How a mail body Herald writes becomes MIME.

Plain text alone reads badly once it leaves Gmail. When Gmail sends a
text/plain message it hard-wraps it at about 72 characters. Outlook, which
many recipients use, then shows every paragraph broken mid-sentence. This
happened on 23 Sep 2026 with a draft to two co-authors. A text/html
alternative is sent without re-wrapping, so every body goes out as both: the
plain text as written, and an HTML rendering of that same text, so clients
reflow it themselves.

The HTML is derived rather than authored so the two can never disagree:
- blank lines separate blocks
- a block whose every line starts with "- " becomes a list
- any other block becomes a paragraph, with its internal line breaks kept
"""

from __future__ import annotations

import html
from email.message import EmailMessage


def to_html(text: str) -> str:
    blocks = [b for b in text.replace("\r\n", "\n").split("\n\n") if b.strip()]
    parts = []
    for block in blocks:
        lines = [ln for ln in block.strip("\n").split("\n")]
        if lines and all(ln.lstrip().startswith("- ") for ln in lines):
            items = "".join(f"<li>{html.escape(ln.lstrip()[2:])}</li>" for ln in lines)
            parts.append(f"<ul>{items}</ul>")
        else:
            parts.append("<p>" + "<br>".join(html.escape(ln) for ln in lines) + "</p>")
    return "<div>" + "".join(parts) + "</div>"


def set_body(msg: EmailMessage, text: str) -> None:
    """Give `msg` a plain body plus its HTML rendering. Attach files after."""
    msg.set_content(text)
    msg.add_alternative(to_html(text), subtype="html")
