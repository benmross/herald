"""The pictures in a message, saved where a cycle's session can look at them.

A flyer mailed as a single image carries its date, time and room in pixels and
nothing in its text. On 24 September 2026 a corporate events round-up arrived
with no event text in its body at all, and on 13 October a hackathon notice was
dismissed from 85 characters of preview. Reading every message in full has to
include what it shows.

Three kinds of picture, two of them risky to fetch:

- **Attached and inline parts** come from Gmail itself. No third party learns
  anything.
- **Linked images** live on the sender's server. Fetching one tells the sender
  the message was opened, usually by a per-recipient URL, so it is a setting
  (`scout.mail_images.remote`) that is off unless the user turns it on.
- A linked image is also a URL chosen by whoever wrote the mail, fetched by an
  unattended job on the user's own network. So only public addresses on the
  standard web ports are fetched, every redirect is checked the same way, and
  the response has to be an image under a size cap. Without that, a message
  could make this machine send a request to a service on the user's LAN.

What comes back is filtered to what a person would call a picture: tracking
pixels, spacers, dividers and icons are dropped by size and proportion, and a
banner already seen in an earlier message is not read a second time.
"""

from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import json
import pathlib
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

MAX_BYTES = 6_000_000
TIMEOUT = 8
MIN_SIDE = 110            # smaller than this is an icon or a pixel
MIN_AREA = 40_000
MAX_ASPECT = 7.0          # dividers and spacers are long and thin
LONG_SIDE = 1400          # downscale: text on a flyer is still legible here
SEEN_KEEP = 4000
_TRACKER = re.compile(r"(?i)(?:/open[/?.]|pixel|beacon|/track|1x1|spacer|/o\.gif|/wf/open|"
                      r"transparent\.(?:gif|png))")
_UA = "Mozilla/5.0 (compatible; mail image reader)"


def _public(host: str) -> bool:
    """True only if every address the name resolves to is on the public internet."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            if not ipaddress.ip_address(info[4][0]).is_global:
                return False
        except ValueError:
            return False
    return True


def allowed_url(url: str) -> bool:
    """A linked image this job is willing to request."""
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    if u.port not in (None, 80, 443) or u.username or u.password:
        return False
    return _public(u.hostname)


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl):
            raise urllib.error.URLError("redirect to a disallowed address")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_remote(url: str) -> bytes | None:
    if not allowed_url(url):
        return None
    opener = urllib.request.build_opener(_CheckedRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "image/*"})
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            if not (resp.headers.get_content_type() or "").startswith("image/"):
                return None
            data = resp.read(MAX_BYTES + 1)
    except (OSError, ValueError):
        return None
    return data if len(data) <= MAX_BYTES else None


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def worth_reading(width: int, height: int) -> bool:
    if min(width, height) < MIN_SIDE or width * height < MIN_AREA:
        return False
    return max(width, height) / max(1, min(width, height)) <= MAX_ASPECT


def _normalise(raw: bytes) -> tuple[bytes, str, int, int] | None:
    """(bytes, extension, width, height) for a picture worth reading, else None."""
    try:
        from PIL import Image  # noqa: PLC0415 -- optional; without it nothing is kept
    except ImportError:
        return None
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:                                      # noqa: BLE001
        return None
    if getattr(im, "n_frames", 1) > 1:
        im.seek(0)
    w, h = im.size
    if not worth_reading(w, h):
        return None
    if max(w, h) > LONG_SIDE:
        im.thumbnail((LONG_SIDE, LONG_SIDE))
    if im.mode not in ("RGB", "L"):
        bg = Image.new("RGB", im.size, "white")
        rgba = im.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        im = bg
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82, optimize=True)
    return buf.getvalue(), "jpg", im.size[0], im.size[1]


class Seen:
    """Hashes of pictures already put in front of a session.

    A sender's header banner is in every message they send. Reading it once is
    understanding it; reading it every pass is the recomputation the design
    exists to avoid.
    """

    def __init__(self, path: pathlib.Path):
        self.path = path
        try:
            self.hashes: list[str] = json.loads(path.read_text())
        except (OSError, ValueError):
            self.hashes = []
        self._set = set(self.hashes)

    def new(self, digest: str) -> bool:
        if digest in self._set:
            return False
        self._set.add(digest)
        self.hashes.append(digest)
        return True

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.hashes[-SEEN_KEEP:]))


def collect(found: list[dict], dest: pathlib.Path, *, seen: Seen, remote: bool,
            limit: int, fetch_attachment=None) -> tuple[list[dict], dict]:
    """Save the readable pictures out of `mailtext.images()` into `dest`.

    Returns (saved, skipped): saved is [{"path", "width", "height", "alt",
    "source"}], skipped counts why the rest were left out, so a snapshot can
    say "4 linked images not fetched" and never imply there were none.
    """
    saved: list[dict] = []
    skipped = {"small": 0, "seen": 0, "remote_off": 0, "failed": 0, "over_limit": 0}
    tried: set[str] = set()
    for img in found:
        if len(saved) >= limit:
            skipped["over_limit"] += 1
            continue
        raw = None
        if img["kind"] == "part":
            source = img.get("filename") or "inline image"
            try:
                if img.get("data"):
                    raw = _b64(img["data"])
                elif img.get("attachment_id") and fetch_attachment:
                    raw = fetch_attachment(img["attachment_id"])
            except Exception:                              # noqa: BLE001
                raw = None
        else:
            url = img["url"]
            source = urllib.parse.urlsplit(url).hostname or "linked image"
            if url in tried:
                continue
            tried.add(url)
            w, h = img.get("width"), img.get("height")
            if (w and w < MIN_SIDE) or (h and h < MIN_SIDE) or _TRACKER.search(url):
                skipped["small"] += 1
                continue
            if not remote:
                skipped["remote_off"] += 1
                continue
            raw = fetch_remote(url)
        if not raw:
            skipped["failed"] += 1
            continue
        norm = _normalise(raw)
        if not norm:
            skipped["small"] += 1
            continue
        data, ext, w, h = norm
        digest = hashlib.sha256(data).hexdigest()
        if not seen.new(digest):
            skipped["seen"] += 1
            continue
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / f"{len(saved) + 1}.{ext}"
        path.write_bytes(data)
        saved.append({"path": str(path), "width": w, "height": h,
                      "alt": img.get("alt") or "", "source": source})
    return saved, {k: v for k, v in skipped.items() if v}
