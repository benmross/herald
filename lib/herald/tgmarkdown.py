"""Keep Markdown attachment content visible in Telegram's built-in viewer.

The viewer can silently omit headings below level two, task states, raw HTML
and display math. These are delivery-only substitutions, based on a rendered
diagnostic document. This is not a general Markdown renderer: ordinary Markdown
stays intact, HTML is readable as source, and display math becomes a code block.
Code examples and comments are left alone. No source file is rewritten.
"""

from __future__ import annotations

import html
import re


def for_viewer(source: str) -> str:
    """Return a conservative, idempotent compatibility copy of Markdown."""
    # Protect code before looking for syntax. Pick a marker absent from the
    # input so user text can never be mistaken for one of our saved regions.
    marker = "\x00HERALD_MD_"
    while marker in source:
        marker += "_"
    saved: list[str] = []

    def save(value: str) -> str:
        # Keep the following line at the beginning of a line while masked.
        ending = "\r\n" if value.endswith("\r\n") else "\n" if value.endswith("\n") else ""
        if ending:
            value = value[:-len(ending)]
        saved.append(value)
        return f"{marker}{len(saved) - 1}\x00{ending}"

    lines = source.splitlines(keepends=True)
    protected: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Quote prefixes belong to the block, including on its closing fence.
        opening = re.match(r"^( {0,3}(?:>[ \t]? {0,3})*)(`{3,}|~{3,})([^\r\n]*)", line)
        if opening and (opening[2][0] != "`" or "`" not in opening[3]):
            prefix, fence = opening[1], opening[2]
            closing = re.compile(r"^" + re.escape(prefix) + re.escape(fence[0])
                                 + "{" + str(len(fence)) + r",}[ \t]*(?:\r?\n)?$")
            end = i + 1
            while end < len(lines):
                end += 1
                if closing.match(lines[end - 1]):
                    break
            protected.append(save("".join(lines[i:end])))
            i = end
            continue
        if line.startswith(("    ", "\t")):
            protected.append(save(line))
        else:
            protected.append(line)
        i += 1
    text = "".join(protected)
    # A span may have multiple backticks and may cross a line boundary.
    text = re.sub(r"(?<!`)(`+)(?!`)(.*?)(?<!`)\1(?!`)",
                  lambda m: save(m[0]), text, flags=re.DOTALL)
    # Comments are intentionally hidden; don't expose their contents.
    text = re.sub(r"<!--[\s\S]*?(?:-->|\Z)", lambda m: save(m[0]), text)

    def math_block(match: re.Match) -> str:
        body = match[1].strip("\r\n ")
        fence = "```"
        while fence in body:
            fence += "`"
        return save(f"\n{fence}latex\n{body}\n{fence}\n\n")

    text = re.sub(r"(?m)^ {0,3}\$\$([\s\S]*?)\$\$[ \t]*(?=\r?$)", math_block, text)
    text = re.sub(r"(?m)^ {0,3}\\\[([\s\S]*?)\\\][ \t]*(?=\r?$)", math_block, text)
    text = re.sub(r"(?m)^( {0,3}(?:>[ \t]* {0,3})*)#{3,6}(?=[ \t]|$)",
                  r"\1##", text)
    text = re.sub(r"(?m)^([ \t]*(?:>[ \t]*)*(?:[-+*]|\d+[.)])[ \t]+)"
                  r"\[([ xX])\](?=[ \t]|$)",
                  lambda m: m[1] + ("☐" if m[2] == " " else "☑"), text)
    # Escape tags, rather than stripping them and guessing where their text
    # belongs. This also keeps summaries, attributes and links recoverable.
    text = re.sub(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*?)?\s*/?>",
                  lambda m: html.escape(m[0], quote=False), text)

    # Regions may contain earlier saved regions (e.g. a multiline code span).
    slot = re.compile(re.escape(marker) + r"(\d+)\x00")
    while slot.search(text):
        text = slot.sub(lambda m: saved[int(m[1])], text)
    return text


def attachment(blob: bytes) -> bytes:
    """Transform UTF-8 Markdown; unfamiliar encodings pass through untouched."""
    try:
        source = blob.decode("utf-8")
    except UnicodeDecodeError:
        return blob
    return for_viewer(source).encode("utf-8")
