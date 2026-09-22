"""Push to the user's phone.

ntfy for now. The routing policy (which channel, what priority, given where the user
is and what device is awake) will grow here rather than in callers.
"""

from __future__ import annotations

import json
import pathlib
import urllib.error
import urllib.request
import uuid

from . import config, db, tgtext

PRIORITIES = ("min", "low", "default", "high", "urgent")

# HTTP header values are latin-1. The message body is a UTF-8 payload and can
# say anything, but Title and Tags are headers, so an em dash in a generated
# headline is enough to take down the whole notification -- which is exactly how
# the first dawn cycle failed after doing all its work.
_TRANSLIT = {
    "\u2014": "-", "\u2013": "-", "\u2012": "-", "\u2212": "-",
    "\u2018": "'", "\u2019": "'", "\u201a": ",",
    "\u201c": '"', "\u201d": '"',
    "\u2026": "...", "\u00a0": " ", "\u2022": "*", "\u2192": "->",
}


def _header_safe(text: str) -> str:
    """Make a string survive latin-1 header encoding without turning to mush."""
    for bad, good in _TRANSLIT.items():
        text = text.replace(bad, good)
    return text.encode("latin-1", "replace").decode("latin-1")


def push(title: str, message: str, *, priority: str = "default",
         tags: str | None = None, click: str | None = None,
         actions: list[dict] | None = None, record: bool = True) -> bool:
    """Send one notification. Returns whether it landed.

    `actions` are ntfy action buttons, e.g.
        [{"action": "http", "label": "Yes", "url": "...", "method": "POST"}]
    which is how a notification becomes a one-tap answer instead of a nudge to
    go open something.
    """
    if priority not in PRIORITIES:
        priority = "default"

    base = config.get("notify.ntfy_url", "https://ntfy.sh").rstrip("/")
    topic = config.get("notify.ntfy_topic")
    if not topic:
        # No topic means no channel, not a topic called "None". ntfy topics are
        # public by name, so posting a digest to https://ntfy.sh/None would
        # hand the user's morning to anyone subscribed to that word. A fresh
        # install without Telegram or ntfy has nowhere to push, and the caller
        # already handles "nowhere"; the digest is still written to
        # ledger/digests/.
        if record:
            try:
                with db.session() as con:
                    con.execute(
                        "INSERT INTO notifications (ts, channel, priority, title, body, ok)"
                        " VALUES (?, 'ntfy', ?, ?, ?, 0)",
                        (db.now(), priority, title, message))
            except Exception:                                       # noqa: BLE001
                pass
        return False
    url = f"{base}/{topic}"

    headers = {
        "Title": _header_safe(title)[:200],
        "Priority": priority,
        "Tags": _header_safe(tags or config.get("notify.default_tags", "scroll")),
        "User-Agent": "herald/0.1",
    }
    if click:
        headers["Click"] = _header_safe(click)
    if actions:
        headers["Actions"] = json.dumps(actions)

    ok = False
    try:
        req = urllib.request.Request(url, data=message.encode("utf-8"),
                                     method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = 200 <= r.status < 300
    except (urllib.error.URLError, OSError):
        ok = False

    if record:
        try:
            with db.session() as con:
                con.execute(
                    "INSERT INTO notifications (ts, channel, priority, title, body, ok)"
                    " VALUES (?, 'ntfy', ?, ?, ?, ?)",
                    (db.now(), priority, title, message, int(ok)),
                )
                con.commit()
        except Exception:
            pass  # never let bookkeeping swallow the notification itself
    return ok


# --------------------------------------------------------------------------
# Telegram
#
# ntfy is the right shape for a headline on a watch and the wrong shape for a
# digest: a push is a glance, and anything past a couple of lines is either
# truncated by the client or unread. Telegram takes the long version, keeps it
# scrollable, and is the same thread the user can talk back on.
#
# So the two are not alternatives. ntfy says what happened; Telegram carries the
# thing itself.
# --------------------------------------------------------------------------

#: Kept as an alias; the real value and the chunker live in tgtext.
TELEGRAM_LIMIT = tgtext.LIMIT


def _telegram_api(method: str, **params) -> dict | None:
    token = config.secret("telegram.bot_token")
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = urllib.request.Request(
        url, data=json.dumps(params).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def _telegram_chat_id() -> int | None:
    """Where a cycle-initiated message goes.

    Read only. Discovery belongs to `herald-telegram`, which holds the long-poll:
    Telegram allows exactly one getUpdates consumer per bot and answers everyone
    else with a 409, so a second caller here would conflict with the bridge on
    every digest. The bridge writes the id; this just reads it.

    This is whichever chat the user last messaged the bot from, which in practice
    is a group with topics rather than a DM: the group is where conversations
    actually happen, and a digest landing in a DM nobody reads was the thing
    this fixed on 8 September 2026.
    """
    config.reload()
    return config.secret("telegram.chat_id")


def _updates_thread_id() -> int | None:
    """The group's "Updates" topic -- where a digest or an unprompted push
    lands unless a caller asks for somewhere more specific.

    Same 8 Sep decision: those used to go to the DM by default. Falling back
    to this here, rather than requiring every call site to pass it, is the
    same reasoning as `_telegram_chat_id` -- one place decides the routing
    policy, callers just say what they want to send.
    """
    config.reload()
    return config.secret("telegram.updates_thread_id")


def telegram(text: str, *, record: bool = True,
             thread_id: int | None = None) -> bool:
    """Send the long version. Returns whether it landed.

    `thread_id=None` means "wherever unprompted stuff goes", which is the
    group's Updates topic -- see `_updates_thread_id`. Pass one explicitly
    only to land somewhere else on purpose.

    Sent as HTML that Herald generated itself -- see `lib/herald/tgtext.py`.
    Markdown mode was tried and removed because Telegram's parser is applied to
    text Herald did not write, and two literal asterisks in two URLs formed a
    valid bold span: accepted, silently stripped, both URLs delivered wrong.
    Converting the Markdown here and escaping everything else first means a
    stray asterisk arrives as an asterisk.

    If Telegram still rejects the markup, the same text goes out unformatted
    rather than not at all. That fallback is for a bug in the converter, not
    for anybody's content: content cannot produce a tag any more.
    """
    chat_id = _telegram_chat_id()
    if not chat_id:
        return False
    if thread_id is None:
        thread_id = _updates_thread_id()

    ok = True
    for part in tgtext.chunks(text):
        extra = {"message_thread_id": thread_id} if thread_id else {}
        resp = _telegram_api("sendMessage", chat_id=chat_id,
                             text=tgtext.to_html(part), parse_mode="HTML",
                             link_preview_options={"is_disabled": True}, **extra)
        if not (resp and resp.get("ok")):
            resp = _telegram_api("sendMessage", chat_id=chat_id, text=part,
                                 link_preview_options={"is_disabled": True},
                                 **extra)
        ok = ok and bool(resp and resp.get("ok"))

    if record:
        try:
            with db.session() as con:
                con.execute(
                    "INSERT INTO notifications (ts, channel, priority, title, body, ok)"
                    " VALUES (?, 'telegram', 'default', ?, ?, ?)",
                    (db.now(), text.splitlines()[0][:120] if text else "", text, int(ok)))
                con.commit()
        except Exception:
            pass
    return ok


def _live_topic_thread() -> int | None:
    """The Telegram topic whose turn is running right now, if exactly one is.

    Same source `herald quiz` uses to work out where a quiz should land.
    Ambiguous when two topics are mid-turn, so it says nothing rather than
    guessing.
    """
    try:
        from . import activity  # noqa: PLC0415
        live = [t["key"] for t in activity.active() if t.get("service") == "telegram"]
    except Exception:                                            # noqa: BLE001
        return None
    if len(live) != 1:
        return None
    _, _, thread = live[0].partition(":")
    return int(thread) if thread and thread != "main" else None


def _telegram_upload(method: str, field: str, filename: str, blob: bytes,
                     **params) -> dict | None:
    """POST a file to the Bot API as multipart/form-data.

    `_telegram_api` sends JSON, which cannot carry a file. Rather than pull in
    a dependency for one content type, this builds the body by hand: the Bot
    API wants each parameter as its own part, and the file part needs a
    filename for Telegram to accept it as an upload.
    """
    token = config.secret("telegram.bot_token")
    if not token:
        return None
    boundary = f"----herald{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for key, value in params.items():
        if value is None:
            continue
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n"
            f"{value}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\";"
        f" filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
        .encode())
    parts.append(blob)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def telegram_photo(path, *, caption: str | None = None, thread_id: int | None = None,
                   record: bool = True) -> bool:
    """Send an image or a video to the user. Returns whether it landed.

    Video goes as sendVideo so it plays in the chat rather than arriving as a
    file to download; the kind is taken from the suffix.

    Herald could only ever send words, which is a poor fit for the things it
    increasingly makes: a screenshot, a slide, a chart. A picture the user has
    to open a link for is a picture they look at later, if at all.

    Falls back to sending the file as a document when Telegram refuses it as a
    photo, which it does for anything over 10 MB or with an extreme aspect
    ratio. Better a file they can tap than nothing.
    """
    chat_id = _telegram_chat_id()
    if not chat_id:
        return False
    if thread_id is None:
        # An image is nearly always an answer to something somebody just
        # asked, so it belongs in the topic that asked. Only when no turn is
        # running (a cycle, a scheduled job) does it fall back to Updates,
        # where unprompted pushes go.
        thread_id = _live_topic_thread()
        if thread_id is None:
            thread_id = _updates_thread_id()
    path = pathlib.Path(path)
    blob = path.read_bytes()

    common = {"chat_id": chat_id}
    if thread_id:
        common["message_thread_id"] = thread_id
    if caption:
        common["caption"] = caption[:1024]

    video = path.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv")
    if video:
        common.setdefault("supports_streaming", "true")
    method, field = ("sendVideo", "video") if video else ("sendPhoto", "photo")
    resp = _telegram_upload(method, field, path.name, blob, **common)
    if not (resp and resp.get("ok")):
        resp = _telegram_upload("sendDocument", "document", path.name, blob, **common)
    ok = bool(resp and resp.get("ok"))

    if record:
        try:
            with db.session() as con:
                con.execute(
                    "INSERT INTO notifications (ts, channel, priority, title, body, ok)"
                    " VALUES (?, 'telegram', 'default', ?, ?, ?)",
                    (db.now(), f"image: {path.name}", caption or "", int(ok)))
                con.commit()
        except Exception:
            pass
    return ok


def ask(approval_id: int, text: str, *, yes: str = "Yes, do it",
        no: str = "No", thread_id: int | None = None) -> bool:
    """Put a red-tier action in front of the user as two buttons.

    Pairs with lib/herald/approvals.py: this only asks. The tap is recorded by
    the Telegram bridge and acted on by whichever process is waiting, so a
    notification cannot itself cause anything to happen -- which is the whole
    reason the mechanism exists rather than a "did you mean it?" line in a
    prompt.
    """
    chat_id = _telegram_chat_id()
    if not chat_id:
        return False
    if thread_id is None:
        thread_id = _updates_thread_id()
    extra = {"message_thread_id": thread_id} if thread_id else {}
    resp = _telegram_api(
        "sendMessage", chat_id=chat_id,
        text=tgtext.to_html(text[:tgtext.LIMIT]), parse_mode="HTML",
        link_preview_options={"is_disabled": True},
        reply_markup={"inline_keyboard": [[
            {"text": yes, "callback_data": f"approve:{approval_id}"},
            {"text": no, "callback_data": f"deny:{approval_id}"},
        ]]}, **extra)
    return bool(resp and resp.get("ok"))


# --------------------------------------------------------------------------
# One notification, not two.
#
# the user was getting an ntfy push and a Telegram message for the same event, which
# is one more buzz than the event deserves. Telegram is where the user can actually
# read the thing and reply to it, so it is the channel; ntfy stays only as the
# way to reach them when Telegram is the thing that is broken.
# --------------------------------------------------------------------------

def tell(title: str, body: str, *, priority: str = "default",
         tags: str | None = None, click: str | None = None,
         thread_id: int | None = None) -> str:
    """Reach the user once. Returns the channel that carried it.

    Telegram first. ntfy only if Telegram failed, because a fallback that always
    fires is not a fallback, it is a second notification.
    """
    # Bold, not italic: a digest headline is the one line that has to be
    # readable at a glance on a watch.
    message = f"**{title}**\n\n{body}" if title else body
    if telegram(message, thread_id=thread_id):
        return "telegram"
    if push(title or "Herald", body, priority=priority, tags=tags, click=click):
        return "ntfy (telegram unreachable)"
    return "nowhere"
