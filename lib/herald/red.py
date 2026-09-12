"""The only module that carries out a red action.

Red, in the constitution: anything another person sees, anything irreversible,
anything that spends money. For most of Herald's life that tier was a sentence
in a prompt. `approvals.py` was built to make it a machine-checkable gate, and
an audit on 12 Sep 2026 found it had never fired once. Nothing was wired to it.

This is the wiring. A red action happens in exactly one way:

    approval_id = red.request("mail.send", payload, actor="telegram")
    # the user's phone shows exactly what will happen, with Yes and No
    state = approvals.wait(approval_id)
    if state == "approved":
        red.execute(approval_id)

or, in one call, `red.run(...)`. Three properties are the point, and each one
closes a specific way a rule-in-a-prompt fails.

**The tap text is built from the payload, never from the caller's description.**
A session talked into something by an email it read could ask to "send the lunch
plan to Mum" while the payload mails something else to someone else. So what the
user approves is rendered here, from the exact recipients, subject and body that
will go out. The caller does not get to summarise its own request.

**An approval is consumed atomically before the action runs.** `execute` moves
the row from approved to done in one conditional UPDATE and proceeds only if it
won that race. One tap, one action, however many processes try to act on it. If
the action then fails, the approval stays consumed: failing closed means asking
again, and the other direction means an old tap can be replayed.

**Approving and acting are different processes.** The Telegram bridge records
the tap and does nothing else; this module, in the process that asked, does the
work. A compromised session can request an action. It cannot approve one,
because approving means a tap from the owner's Telegram account, which the
bridge checks by user id.

`tools/check.py` holds every other file in the program to this: no `.send(`, no
permission changes, no permanent deletes anywhere but here.
"""

from __future__ import annotations

import base64
import json
from email.message import EmailMessage

from . import approvals, db, google, notify

TAP_TIMEOUT = 600


# --------------------------------------------------------------------------
# Kinds. Each has a renderer (what the user sees) and a performer (what runs).
# --------------------------------------------------------------------------

def _render_mail(p: dict) -> str:
    body = (p.get("body") or "").strip()
    preview = body if len(body) <= 700 else body[:700] + "\n[...]"
    lines = ["Send this email?", "", f"To: {p.get('to', '?')}"]
    if p.get("cc"):
        lines.append(f"Cc: {p['cc']}")
    lines += [f"Subject: {p.get('subject', '')}", "", preview]
    return "\n".join(lines)


def _perform_mail(p: dict) -> tuple[str, str]:
    for field in ("to", "subject", "body"):
        if not p.get(field):
            raise ValueError(f"mail.send payload is missing {field!r}")
    msg = EmailMessage()
    msg["To"] = p["to"]
    msg["Subject"] = p["subject"]
    if p.get("cc"):
        msg["Cc"] = p["cc"]
    if p.get("in_reply_to"):
        msg["In-Reply-To"] = p["in_reply_to"]
        msg["References"] = p["in_reply_to"]
    msg.set_content(p["body"])
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body = {"raw": raw}
    if p.get("thread_id"):
        body["threadId"] = p["thread_id"]
    sent = (google.service("gmail", "v1").users().messages()
            .send(userId="me", body=body).execute())
    return (f"sent '{p['subject'][:60]}' to {p['to']}", f"gmail:{sent.get('id')}")


def _render_drill(_p: dict) -> str:
    return ("Drill: nothing will be sent or changed.\n\n"
            "Tap Yes to confirm the red-tier approval path works end to end: "
            "this message reached you, your tap is recorded, and the waiting "
            "process acts on it exactly once.")


def _perform_drill(_p: dict) -> tuple[str, str]:
    return ("drill: approval path exercised end to end, no outward effect", "drill")


KINDS = {
    "mail.send": (_render_mail, _perform_mail),
    "drill": (_render_drill, _perform_drill),
}


# --------------------------------------------------------------------------
# The flow
# --------------------------------------------------------------------------

def render(kind: str, payload: dict) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown red action {kind!r}; known: {sorted(KINDS)}")
    return KINDS[kind][0](payload)


def request(kind: str, payload: dict, *, actor: str,
            thread_id: int | None = None) -> int:
    """File the approval and put it on the user's phone. Returns its id."""
    text = render(kind, payload)
    target = payload.get("to") if kind == "mail.send" else None
    approval_id = approvals.request(actor=actor, kind=kind, target=target,
                                    summary=text.splitlines()[0], payload=payload)
    if not notify.ask(approval_id, text, yes="Yes, do it", no="No",
                      thread_id=thread_id):
        # An approval nobody can see is an approval that will only ever expire.
        # Say so now rather than letting the caller wait out the timeout.
        with db.session() as con:
            con.execute("UPDATE approvals SET state = ? WHERE id = ? AND state = ?",
                        (approvals.EXPIRED, approval_id, approvals.PENDING))
            con.commit()
        raise RuntimeError("could not deliver the approval request to Telegram")
    return approval_id


def execute(approval_id: int, *, actor: str = "red") -> dict:
    """Carry out an approved action, exactly once."""
    row = approvals.get(approval_id)
    if row is None:
        raise PermissionError(f"approval {approval_id} does not exist")
    kind = row["kind"]
    if kind not in KINDS:
        raise PermissionError(f"approval {approval_id} is for unknown kind {kind!r}")

    # Claim it. Only one process can move approved -> done, and only a row the
    # user actually approved can be moved at all.
    with db.session() as con:
        won = con.execute(
            "UPDATE approvals SET state = ? WHERE id = ? AND state = ?",
            (approvals.DONE, approval_id, approvals.APPROVED)).rowcount
        con.commit()
    if not won:
        state = (approvals.get(approval_id) or {}).get("state", "missing")
        raise PermissionError(
            f"approval {approval_id} is {state}, not approved; nothing done")

    payload = json.loads(row["payload"] or "{}")
    summary, ref = KINDS[kind][1](payload)
    with db.session() as con:
        db.record_action(con, actor=actor, tier="red", kind=kind,
                         target=row["target"], summary=summary, ref=ref,
                         approval_id=approval_id)
        con.commit()
    return {"approval_id": approval_id, "kind": kind, "summary": summary, "ref": ref}


def run(kind: str, payload: dict, *, actor: str, timeout: int = TAP_TIMEOUT,
        thread_id: int | None = None) -> dict:
    """Request, wait for the tap, and act if approved."""
    approval_id = request(kind, payload, actor=actor, thread_id=thread_id)
    state = approvals.wait(approval_id, timeout=timeout)
    if state != approvals.APPROVED:
        return {"approval_id": approval_id, "kind": kind, "state": state,
                "summary": f"not done: {state}"}
    result = execute(approval_id, actor=actor)
    result["state"] = "done"
    return result
