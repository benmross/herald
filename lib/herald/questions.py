"""Questions in the asking Telegram topic, with durable answers and consent.

Ordinary answers are choices or explicit replies to the question message.
Consent requires the owner's Approve button. Neither grants a safety override
nor executes the action described in the question.
"""

from __future__ import annotations

import json
import time

from . import approvals, config, db, notify, private, red, tgtext

CALLBACK = "question"


def get(question_id: int) -> dict:
    with db.session() as con:
        row = con.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
        if row is None:
            raise ValueError("question does not exist")
        result = dict(row)
        if result["state"] == "pending" and result["expires_at"] <= time.time():
            con.execute("UPDATE questions SET state='expired' WHERE id=? AND state='pending'",
                        (question_id,))
            if result["approval_id"]:
                con.execute("UPDATE approvals SET state='expired' WHERE id=? AND state='pending'",
                            (result["approval_id"],))
            result["state"] = "expired"
    result["options"] = json.loads(result["options"])
    return result


def result(question_id: int) -> dict:
    row = get(question_id)
    # Consume affirmative consent through red.py exactly once, recording a
    # receipt. A crash after the tap but before consumption is recoverable.
    if row["approval_id"] and row["state"] == "answered" and row["answer"] == "Approve":
        approval = approvals.get(row["approval_id"])
        if approval and approval["state"] == approvals.APPROVED:
            try:
                red.execute(row["approval_id"], actor=row["actor"])
            except PermissionError:
                # Another reader may have claimed the same receipt.
                if (approvals.get(row["approval_id"]) or {}).get("state") != approvals.DONE:
                    raise
        receipt = db.query("SELECT id FROM actions WHERE approval_id=? AND kind='consent'",
                           (row["approval_id"],))
        row["approved"] = bool(receipt)
    else:
        row["approved"] = False
    return row


def request(question: str, *, options: list[str] | None = None, approval: bool = False,
            actor: str = "session", timeout: int = 86400, wake: bool = True,
            thread_id: int | None = None) -> dict:
    private.refuse("A Telegram question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("a question needs text")
    if not 1 <= timeout <= 604800:
        raise ValueError("timeout must be between 1 second and 7 days")
    options = list(options or [])
    if approval and options:
        raise ValueError("approval questions use fixed Approve / Decline buttons")
    if approval:
        options = ["Approve", "Decline"]
    if len(options) > 8 or len(set(options)) != len(options):
        raise ValueError("use at most eight distinct options")
    if any(not isinstance(o, str) or not o.strip() or len(o) > 80 for o in options):
        raise ValueError("each option must contain 1 to 80 characters")
    text = red.render("consent", {"question": question}) if approval else question
    if not approval:
        text += "\n\nTap a choice or reply to this message." if options else "\n\nReply to this message."
    if len(text.encode("utf-16-le")) // 2 > tgtext.LIMIT - 100:
        raise ValueError("the complete question must fit one Telegram message")
    chat = notify._telegram_chat_id()
    if not chat:
        raise RuntimeError("Telegram is not configured")
    if thread_id is None:
        thread_id = notify._resolve(notify._own_topic_thread(chat))
    with db.session() as con:
        approval_id = None
        if approval:
            approval_id = con.execute(
                "INSERT INTO approvals (ts,actor,kind,target,summary,payload,state)"
                " VALUES (?,?,'consent',?,?,?,'pending')",
                (db.now(), actor, f"telegram:{chat}:{thread_id or 'main'}", question,
                 json.dumps({"question": question}))).lastrowid
        question_id = con.execute(
            "INSERT INTO questions (ts,actor,question,options,chat_id,thread_id,approval_id,wake,expires_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (db.now(), actor, question, json.dumps(options), chat, thread_id,
             approval_id, int(wake), time.time() + timeout)).lastrowid
    extra = {"message_thread_id": thread_id} if thread_id else {}
    try:
        response = notify._telegram_api(
            "sendMessage", chat_id=chat, text=text,
            link_preview_options={"is_disabled": True},
            reply_markup={"inline_keyboard": [
                [{"text": option, "callback_data": f"{CALLBACK}:{question_id}:{i}"}]
                for i, option in enumerate(options)]}, **extra)
        if not response or not response.get("ok") or not response.get("result", {}).get("message_id"):
            raise RuntimeError("could not deliver the question to Telegram")
        with db.session() as con:
            con.execute("UPDATE questions SET message_id=? WHERE id=?",
                        (response["result"]["message_id"], question_id))
    except Exception:
        with db.session() as con:
            con.execute("UPDATE questions SET state='expired' WHERE id=?", (question_id,))
            if approval_id:
                con.execute("UPDATE approvals SET state='expired' WHERE id=? AND state='pending'",
                            (approval_id,))
        raise
    return get(question_id)


def answer(question_id: int, text: str, *, sender: int, message: dict,
           choice: int | None = None) -> dict | None:
    """Called by the bridge only. Bind the owner, chat, topic and message."""
    if sender != config.secret("telegram.user_id"):
        return None
    row = get(question_id)
    if ((message.get("chat") or {}).get("id") != row["chat_id"]
            or message.get("message_id") != row["message_id"]
            or (message.get("message_thread_id") or None) != row["thread_id"]):
        return None
    if choice is not None:
        if not 0 <= choice < len(row["options"]):
            return None
        text = row["options"][choice]
    elif row["approval_id"]:
        return None  # A text reply, including "yes", cannot grant consent.
    if not text.strip():
        return None
    with db.session() as con:
        won = con.execute(
            "UPDATE questions SET state='answered',answer=?,answered_at=?,answered_by=?"
            " WHERE id=? AND state='pending' AND expires_at>?",
            (text, db.now(), f"telegram:{sender}", question_id, time.time())).rowcount
        if not won:
            return None
        if row["approval_id"]:
            state = approvals.APPROVED if choice == 0 else approvals.DENIED
            won = con.execute(
                "UPDATE approvals SET state=?,decided_at=?,decided_by=? WHERE id=? AND state='pending'",
                (state, db.now(), f"telegram:{sender}", row["approval_id"])).rowcount
            if not won:
                raise PermissionError("the consent request is no longer pending")
    return result(question_id)


def reply(msg: dict) -> dict | None:
    original = msg.get("reply_to_message") or {}
    chat = (msg.get("chat") or {}).get("id")
    rows = db.query("SELECT id FROM questions WHERE chat_id=? AND message_id=?",
                    (chat, original.get("message_id")))
    if not rows:
        return None
    original = {**original, "chat": msg.get("chat"),
                "message_thread_id": msg.get("message_thread_id")}
    return answer(rows[0]["id"], msg.get("text") or "",
                  sender=(msg.get("from") or {}).get("id"), message=original)


def wait(question_id: int) -> dict:
    with db.session() as con:
        con.execute("UPDATE questions SET wake=0 WHERE id=? AND state='pending'", (question_id,))
    while True:
        row = result(question_id)
        if row["state"] != "pending":
            return row
        time.sleep(1)
