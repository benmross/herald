"""What Herald actually sent to Telegram and what it received, in order.

Before this existed (23 Nov 2026) the only records were message ids on a turn
row and an outbox that forgets a message once it is delivered. The user asked
whether a reply had reached them, and the wording that was sent could not be
shown: it had to be inferred from the session transcript.

Every Bot API call that changes what the user sees goes through `sent()`, from
`tgapi.call` and from the file upload in notify.py, with Telegram's answer:
the message id on success, the error on failure. The bridge records each
update it receives with `received()`. Rows live in the `telegram_log` table
and are dropped after `telegram.log_days`.

A private session is unrecorded, so its text never reaches this table: the
row keeps the time, method, ids and outcome, with `private` set and no text.
The bridge says which topics are private through `private_topic`; any process
that is itself inside a private session records no text at all.

Logging never raises. A send that worked must not fail because its record
could not be written.
"""
from __future__ import annotations

import datetime as dt
import json
import sys

from . import config, db, private

# Calls that show the user nothing, or that only read.
SKIP = {"getUpdates", "sendChatAction", "getFile", "getMe", "getChat", "getForumTopicIconStickers",
        "setMyCommands", "getMyCommands", "deleteMyCommands", "getWebhookInfo", "deleteWebhook"}

# Set by the bridge: (chat_id, thread_id) -> is that topic in a private session?
private_topic = None


def _is_private(chat_id, thread_id) -> bool:
    if private.active():
        return True
    try:
        return bool(private_topic and private_topic(chat_id, thread_id))
    except Exception:  # noqa: BLE001 -- unknown means unrecorded
        return True


def _write(direction, method, chat_id, thread_id, message_id, ok, error, text, extra, hidden) -> None:
    try:
        with db.session() as con:
            con.execute(
                """INSERT INTO telegram_log
                   (ts, direction, method, chat_id, thread_id, message_id, ok, error, text, extra, private)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (dt.datetime.now(config.tz()).isoformat(timespec="milliseconds"), direction, method,
                 chat_id, thread_id, message_id, None if ok is None else int(bool(ok)), error,
                 None if hidden else text,
                 None if hidden or not extra else json.dumps(extra, ensure_ascii=False), int(hidden)))
    except Exception as exc:  # noqa: BLE001
        print(f"telegram log skipped ({type(exc).__name__})", file=sys.stderr, flush=True)


def sent(method: str, params: dict, response, *, filename: str | None = None) -> None:
    """One outgoing call and Telegram's answer to it."""
    if method in SKIP:
        return
    params = params or {}
    chat_id, thread_id = params.get("chat_id"), params.get("message_thread_id")
    result = (response or {}).get("result")
    message_id = params.get("message_id")
    if isinstance(result, dict) and result.get("message_id"):
        message_id = result["message_id"]
        thread_id = thread_id or result.get("message_thread_id")
    ok = bool(response and response.get("ok"))
    error = None
    if not ok:
        error = ("no response" if response is None else
                 f"{response.get('error_code', '')} {response.get('description', '')}".strip())
    text = params.get("text") if params.get("text") is not None else params.get("caption")
    extra = {k: params[k] for k in ("parse_mode", "reply_to_message_id", "name", "icon_custom_emoji_id",
                                    "callback_query_id", "show_alert") if params.get(k) is not None}
    markup = params.get("reply_markup")
    if markup:
        if isinstance(markup, str):
            try:
                markup = json.loads(markup)
            except ValueError:
                markup = {}
        buttons = [b.get("text") for row in (markup or {}).get("inline_keyboard") or [] for b in row
                   if isinstance(b, dict)]
        if buttons:
            extra["buttons"] = buttons
    if filename:
        extra["file"] = filename
    _write("out", method, chat_id, thread_id, message_id, ok, error,
           None if text is None else str(text), extra, _is_private(chat_id, thread_id))


def received(update: dict) -> None:
    """One update from Telegram: a message, an edit, or a button tap."""
    cb = update.get("callback_query")
    if cb:
        msg = cb.get("message") or {}
        chat_id, thread_id = (msg.get("chat") or {}).get("id"), msg.get("message_thread_id")
        _write("in", "callback_query", chat_id, thread_id, msg.get("message_id"), None, None,
               cb.get("data"), {"from": (cb.get("from") or {}).get("id")}, _is_private(chat_id, thread_id))
        return
    kind = next((k for k in ("message", "edited_message") if update.get(k)), None)
    if not kind:
        return
    msg = update[kind]
    chat_id, thread_id = (msg.get("chat") or {}).get("id"), msg.get("message_thread_id")
    extra = {"from": (msg.get("from") or {}).get("id")}
    for media in ("photo", "document", "voice", "audio", "video", "video_note", "sticker", "location"):
        if msg.get(media):
            extra.setdefault("media", []).append(media)
    if (msg.get("document") or {}).get("file_name"):
        extra["file"] = msg["document"]["file_name"]
    if msg.get("reply_to_message"):
        extra["reply_to_message_id"] = msg["reply_to_message"].get("message_id")
    text = msg.get("text") if msg.get("text") is not None else msg.get("caption")
    _write("in", kind, chat_id, thread_id, msg.get("message_id"), None, None, text, extra,
           _is_private(chat_id, thread_id))


def prune(days: int | None = None) -> int:
    """Drop rows older than `telegram.log_days` (default 30). Returns how many."""
    days = config.get("telegram.log_days", 30) if days is None else days
    if not days or days <= 0:
        return 0
    cutoff = (dt.datetime.now(config.tz()) - dt.timedelta(days=days)).isoformat(timespec="milliseconds")
    try:
        with db.session() as con:
            return con.execute("DELETE FROM telegram_log WHERE ts < ?", (cutoff,)).rowcount
    except Exception:  # noqa: BLE001
        return 0


def since(value: str) -> str:
    """'90m', '6h', '2d' or an ISO time, as an ISO time in the configured zone."""
    value = value.strip()
    unit = {"m": "minutes", "h": "hours", "d": "days"}.get(value[-1:].lower())
    if unit and value[:-1].replace(".", "", 1).isdigit():
        return (dt.datetime.now(config.tz()) - dt.timedelta(**{unit: float(value[:-1])})).isoformat(timespec="milliseconds")
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=config.tz())
    return parsed.astimezone(config.tz()).isoformat(timespec="milliseconds")


def rows(*, since_ts: str | None = None, until_ts: str | None = None, chat_id=None, thread_id=None,
         message_id=None, direction: str | None = None, grep: str | None = None, limit: int = 50) -> list[dict]:
    """Matching rows, oldest first; the newest `limit` of them."""
    where, args = [], []
    for clause, value in (("ts >= ?", since_ts), ("ts <= ?", until_ts), ("chat_id = ?", chat_id),
                          ("message_id = ?", message_id), ("direction = ?", direction)):
        if value is not None:
            where.append(clause)
            args.append(value)
    if thread_id is not None:
        where.append("thread_id IS NULL" if thread_id == "main" else "thread_id = ?")
        if thread_id != "main":
            args.append(thread_id)
    if grep:
        where.append("text LIKE ?")
        args.append(f"%{grep}%")
    sql = "SELECT * FROM telegram_log" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    with db.session() as con:
        return [dict(r) for r in con.execute(sql, (*args, limit)).fetchall()][::-1]
