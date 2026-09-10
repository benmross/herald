"""Markdown into Telegram-safe HTML.

Herald writes Markdown everywhere -- the morning digest is a Markdown document,
and a session answering a question writes the way it writes. Telegram renders
none of it by default, so for a while every digest arrived with literal `**`
and `#` in it.

The obvious fix, `parse_mode="Markdown"`, was tried and removed, for a good
reason worth recording because it is not the obvious one. Telegram's Markdown
parser is applied to text Herald did not write: mail subjects, scraped event
titles, URLs. Two literal asterisks anywhere in one message -- two URLs each
shaped `RecNumAndPort=119388*1` is how it was found -- form a *valid* bold
span. Telegram accepts the message, silently strips both asterisks, and both
URLs arrive wrong. A fallback that retries on rejection cannot catch that,
because nothing was rejected. So Markdown came out entirely and messages became
flat text.

This is the third answer, and the difference is where the parsing happens.
Herald parses the Markdown itself and emits HTML, and Telegram's HTML mode only
has to honour tags that are already correct:

1. Code spans and fenced blocks come out first, so an asterisk inside `code`
   is never emphasis.
2. Everything that remains is HTML-escaped, so no character in anybody else's
   text can become markup.
3. Only then are the tags this module decided on put back.

A stray asterisk that is not part of a pair this module matched stays a literal
asterisk, because by the time Telegram sees it, it has been escaped. Silent
corruption of the old kind is structurally impossible rather than unlikely.

Telegram's HTML mode supports a short list of tags and nothing else: b, i, u, s,
code, pre, a, blockquote, tg-spoiler. Headings and lists have no equivalent, so
a heading becomes bold and a bullet becomes a bullet character.
"""

from __future__ import annotations

import re


def _esc(text: str) -> str:
    """Escape for HTML *text*, and only the three characters that need it.

    `html.escape` also turns an apostrophe into `&#x27;`, and Telegram's HTML
    parser documents support for `&lt;`, `&gt;`, `&amp;` and `&quot;` -- not
    numeric entities. An apostrophe is the most common character in English
    prose after the letters; getting this wrong shows `it&#x27;s` to the reader
    on every second line.
    """
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc_attr(url: str) -> str:
    """Escape for an attribute value, where a quote does have to go."""
    return _esc(url).replace('"', "&quot;")


#: Telegram's own cap is 4096 UTF-16 code units. Leave room for chunk markers.
LIMIT = 3900

_SENTINEL = "\x00"

_FENCE = re.compile(r"```(?:(\w+)\n)?(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S)
_BOLD_ALT = re.compile(r"__(?=\S)(.+?)(?<=\S)__", re.S)
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.S)
#: Single-marker emphasis, and the fussiest rule here. It must not match the
#: `*` in a URL or in `2 * 3`, so both delimiters have to hug non-space, and a
#: match may not span a blank line.
_ITALIC_STAR = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=\S)\*(?![\w*])")
_ITALIC_UNDER = re.compile(r"(?<![\w_])_(?=[^\s_])([^_\n]+?)(?<=\S)_(?![\w_])")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$", re.M)
_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.M)
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$", re.M)
_HR = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$", re.M)


def _protect(text: str, store: list[str], rendered: str) -> str:
    """Swap a span out for a placeholder that escaping cannot touch."""
    store.append(rendered)
    return f"{_SENTINEL}{len(store) - 1}{_SENTINEL}"


def to_html(markdown: str) -> str:
    """Telegram HTML for a Markdown string. Never raises on odd input."""
    if not markdown:
        return ""
    store: list[str] = []
    text = markdown.replace("\r\n", "\n")

    # 1. Code first, so nothing inside it is ever read as formatting.
    def fence(match: re.Match) -> str:
        body = match.group(2).strip("\n")
        return _protect(match.group(0), store, f"<pre>{_esc(body)}</pre>")

    text = _FENCE.sub(fence, text)

    def inline(match: re.Match) -> str:
        return _protect(match.group(0), store,
                        f"<code>{_esc(match.group(1))}</code>")

    text = _INLINE_CODE.sub(inline, text)

    # 2. Links, before escaping, because a URL is not display text: the label
    #    still gets escaped, and the href is escaped as an attribute.
    def link(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        if not re.match(r"^(https?://|tg://|mailto:)", url, re.I):
            # Not a scheme Telegram will accept; keep it readable instead of
            # emitting a tag it rejects.
            return _protect(match.group(0), store,
                            f"{_esc(label)} ({_esc(url)})")
        return _protect(match.group(0), store,
                        f'<a href="{_esc_attr(url)}">' f"{_esc(label)}</a>")

    text = _LINK.sub(link, text)

    # 3. Now nothing left is markup anybody else wrote.
    text = _esc(text)

    # 4. Block shapes Telegram has no tag for.
    text = _HR.sub("––––––", text)
    text = _HEADING.sub(lambda m: f"<b>{m.group(1).strip()}</b>", text)
    text = _BULLET.sub(lambda m: f"{m.group(1)}• ", text)
    text = _QUOTE.sub(lambda m: f"<blockquote>{m.group(1)}</blockquote>", text)

    # 5. Inline emphasis, longest markers first so ** is not read as two *.
    text = _BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _BOLD_ALT.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _STRIKE.sub(lambda m: f"<s>{m.group(1)}</s>", text)
    text = _ITALIC_STAR.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _ITALIC_UNDER.sub(lambda m: f"<i>{m.group(1)}</i>", text)

    # 6. Put the code and links back.
    def restore(match: re.Match) -> str:
        return store[int(match.group(1))]

    text = re.sub(rf"{_SENTINEL}(\d+){_SENTINEL}", restore, text)

    # Consecutive blockquote lines render as separate quotes otherwise.
    text = re.sub(r"</blockquote>\n<blockquote>", "\n", text)
    return text.strip()


def escape(text: str) -> str:
    """Plain text, safe to drop into an HTML-mode message with no formatting."""
    return _esc(text)


def code_block(text: str) -> str:
    """A whole string as one preformatted block -- the right shape for a live
    progress transcript, which is full of paths and shell commands that should
    stay copyable and should never be read as formatting."""
    return f"<pre>{_esc(text)}</pre>"


def chunks(markdown: str, limit: int = LIMIT) -> list[str]:
    """Split *Markdown* into pieces that each convert to a whole message.

    The source is split rather than the HTML, because splitting rendered HTML
    can cut a tag in half and Telegram rejects the result. Paragraph boundaries
    first, then lines, then a hard cut for a single paragraph that is simply
    too long.
    """
    if len(markdown) <= limit:
        return [markdown]
    out: list[str] = []
    current = ""
    for para in markdown.split("\n\n"):
        if len(current) + len(para) + 2 > limit and current:
            out.append(current.rstrip())
            current = ""
        while len(para) > limit:
            head, _, rest = para[:limit].rpartition("\n")
            if not head:
                head, rest = para[:limit], para[limit:]
            out.append(head)
            para = rest
        current += para + "\n\n"
    if current.strip():
        out.append(current.rstrip())
    return out
