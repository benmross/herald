"""Does a tree of files carry anything about this install's person?

`herald check` asks it of the program's own tracked files, because the
program's repo is public. `herald ext check` asks it of one extension before
somebody publishes it, because an extension is written next to a ledger full
of real data and is the next most likely place for a name, a home path or a
token to end up in public.

The denylist is derived from this install rather than hardcoded: whatever
this user's name, address and credentials are, none of them may appear.
"""

from __future__ import annotations

import getpass
import pathlib
import re
import subprocess

from . import config

#: shapes that are a credential wherever they appear, whoever's they are
SHAPES = [
    (re.compile(r"\b\d{8,}:[A-Za-z0-9_-]{30,}\b"), "a Telegram bot token"),
    (re.compile(r"\b\d+-[a-z0-9]{20,}\.apps\.googleusercontent\.com\b"),
     "a Google OAuth client id"),
    (re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}"), "a Google access token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"), "a GitHub token"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "an API key"),
]


def account_pattern(login: str) -> re.Pattern:
    """Where a login name is evidence of a leak rather than an ordinary word.

    A login is very often a real word -- `runner` on a CI machine, `pi` on a
    Raspberry Pi, `admin`, `dev` -- so matching it as prose is useless. Matching
    it after any slash is not much better: a login called `dev` then flags
    /dev/null, /dev/tty and a GitHub branch called dev.

    What actually leaks is a **home** directory or an address: /home/<login>,
    /Users/<login>, ~login, login@host. Anchoring on those is the difference
    between catching a tracked symlink into somebody's home and flagging half
    the shell scripts in the tree.

    Anything outside a home directory is covered by the home-path needles
    built separately, which match the literal path this install uses.
    """
    account = re.escape(login.lower())
    return re.compile(rf"(?:/home/|/users/|~){account}\b|\b{account}@")


def needles() -> dict[str, str]:
    """{needle: why it is personal}, from this install's config and secrets.

    Name parts match on word boundaries and case-sensitively: a leaked name is
    capitalised ("Ada"), an ordinary word in a docstring is not ("will"). The
    full name and the email match case-insensitively, since they cannot be
    ordinary words. A checker that cries wolf on every docstring is a checker
    somebody switches off.
    """
    out: dict[str, str] = {}
    name = (config.get("user.name") or "").strip()
    if len(name) >= 3:
        out[name.lower()] = "the user's name"
    for part in name.split():
        if len(part) >= 3:
            out[part] = "the user's name"
    for key in ("user.email", "user.school", "user.city"):
        value = str(config.get(key) or "").strip()
        if len(value) >= 5:
            out[value.lower()] = key

    # The home paths, which a name-derived denylist misses entirely: five
    # tracked symlinks once pointed at /home/<user>/.herald/extensions/...,
    # publishing a username and the names of somebody's private extensions
    # without containing their name anywhere.
    for path in (config.HOME, pathlib.Path.home()):
        out[str(path).lower()] = "an absolute path into this user's home"

    def _walk(node, prefix=""):
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(v, f"{prefix}.{k}" if prefix else k)
        elif isinstance(node, str) and len(node) >= 12 and not node.startswith("_"):
            out[node.lower()] = f"a value from secrets.json ({prefix})"

    _walk(config.secrets())
    return out


def remote_urls(root: pathlib.Path) -> set[str]:
    """The repository's own URLs, which are not a leak.

    On a machine where the GitHub account and the login share a name, the
    project's own URL would otherwise trip the account-name needle in every
    install instruction. Blanked before scanning rather than allowlisted per
    file, so a home *path* containing the same name is still caught.
    """
    urls = set()
    out = subprocess.run(["git", "remote", "-v"], cwd=root,
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            url = parts[1].removesuffix(".git")
            urls.add(url.lower())
            if "github.com" in url:
                urls.add(url.lower().split("github.com", 1)[1].lstrip(":/"))
    return urls


def scan(root: pathlib.Path, files: list[str], *, urls: set[str] | None = None,
         allowed: set[str] = frozenset()) -> list[str]:
    """["path:line contains <why>"] for every personal thing in `files`.

    `files` are relative to `root`. A symlink is scanned by its target, since
    a target is content too: five tracked symlinks once pointed into
    $HERALD_HOME/extensions/... and would have published a username.
    """
    found = needles()
    urls = remote_urls(root) if urls is None else urls
    contextual = [(account_pattern(getpass.getuser()),
                   "this machine's account name, in a path or address")]
    hits: list[str] = []
    for rel in files:
        if rel in allowed:
            continue
        path = root / rel
        if path.is_symlink():
            text = str(pathlib.Path.readlink(path))
        elif not path.is_file():
            continue
        else:
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
        lowered = text.lower()
        for url in urls:
            if url:
                lowered = lowered.replace(url, "<this project's repo>")
        for needle, why in found.items():
            # Not \b: a needle that starts with "/" (a home path) has no word
            # boundary in front of it after a space, so \b never matched one.
            pattern = re.compile(rf"(?<!\w){re.escape(needle)}(?!\w)")
            # A lowercase needle scans the lowered text; a capitalised one (a
            # name part) scans the text as written, so "Ada" is a hit and the
            # word "ben" inside prose is not.
            haystack = lowered if needle == needle.lower() else text
            for url in (urls if haystack is text else ()):
                haystack = haystack.replace(url, "<this project's repo>")
            if not pattern.search(haystack):
                continue
            line = next((i + 1 for i, l in enumerate(text.splitlines())
                         if pattern.search(l.lower() if haystack is lowered else l)), 0)
            hits.append(f"{rel}:{line} contains {why}")
        for pattern, why in contextual:
            if pattern.search(lowered):
                line = next((i + 1 for i, l in enumerate(text.splitlines())
                             if pattern.search(l.lower())), 0)
                hits.append(f"{rel}:{line} contains {why}")
        for pattern, why in SHAPES:
            if pattern.search(text):
                hits.append(f"{rel}: looks like {why}")
    return sorted(set(hits))
