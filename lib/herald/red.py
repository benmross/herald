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

**An extension can add a kind, and it gets the same door.** A service only one
person uses (a learning-management system, a self-hosted app) has red actions
of its own, and without a way in here the choice would be between naming that
service in the program and letting its extension act with no tap at all. So an
enabled extension may ship `red.py` beside its manifest, exporting
`KINDS = {"<extension>.<verb>": (render, perform)}`. The prefix is enforced and
a built-in kind cannot be replaced, so an extension can never change what
"mail.send" does. Its performer runs only inside `execute`, after the claim,
and `acting()` lets the code underneath it refuse to run anywhere else.
"""

from __future__ import annotations

import base64
import contextvars
import importlib.util
import json
import sys
from email.message import EmailMessage

from . import approvals, db, google, mailfmt, notify

TAP_TIMEOUT = 600

#: The approval being carried out right now, set only for the duration of a
#: performer inside `execute`. Read it through `acting()`.
_ACTING: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "herald_red_acting", default=None)


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
    mailfmt.set_body(msg, p["body"])
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


def _render_safety_allow(p: dict) -> str:
    rule = p.get("rule", "?")
    lines = [f"Lift the safety rule '{rule}' for {p.get('minutes', 10)} minutes?", ""]
    if p.get("command"):
        lines += ["The blocked command:", str(p["command"])[:600], ""]
    if p.get("why"):
        lines += ["Its reason, in its own words: " + str(p["why"])[:300]]
    return "\n".join(lines)


def _perform_safety_allow(p: dict) -> tuple[str, str]:
    """Write the allowance the guard reads. See lib/herald/safety.py."""
    import os  # noqa: PLC0415
    import time  # noqa: PLC0415
    from . import config, safety  # noqa: PLC0415
    rule = p.get("rule")
    if rule not in safety.RULES:
        raise ValueError(f"unknown safety rule {rule!r}")
    minutes = max(1, min(int(p.get("minutes", 10)), 120))
    d = safety.allow_dir(str(config.HOME))
    os.makedirs(d, mode=0o700, exist_ok=True)
    with open(os.path.join(d, f"{rule}.allow"), "w") as f:
        f.write(str(time.time() + minutes * 60))
    return (f"lifted safety rule {rule} for {minutes} min", f"safety:{rule}")


KINDS = {
    "mail.send": (_render_mail, _perform_mail),
    "drill": (_render_drill, _perform_drill),
    "safety.allow": (_render_safety_allow, _perform_safety_allow),
}


_EXTENSION_KINDS: dict | None = None


def _extension_kinds() -> dict:
    """Kinds shipped by enabled extensions, loaded once per process.

    This is the one place the manifest-is-read-never-imported rule gives way,
    and only when a red action is actually being rendered or carried out. A
    `red.py` that fails to import contributes nothing and says so on stderr:
    a broken extension must not take `mail.send` down with it.
    """
    global _EXTENSION_KINDS
    if _EXTENSION_KINDS is not None:
        return _EXTENSION_KINDS
    from . import extensions  # noqa: PLC0415
    found: dict = {}
    for ext in extensions.enabled():
        path = ext.path / "red.py"
        if not path.exists():
            continue
        lib = str(ext.path / "lib")
        if lib not in sys.path:
            sys.path.insert(0, lib)
        try:
            spec = importlib.util.spec_from_file_location(
                f"herald_ext_red_{ext.path.name}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            declared = dict(getattr(module, "KINDS", {}))
        except Exception as exc:  # noqa: BLE001
            print(f"red: extension {ext.name} red.py did not load: {exc}",
                  file=sys.stderr)
            continue
        for kind, pair in declared.items():
            if not kind.startswith(f"{ext.name}.") or kind in KINDS:
                print(f"red: ignoring kind {kind!r} from extension {ext.name}: "
                      f"a kind must be named '{ext.name}.<verb>'", file=sys.stderr)
                continue
            found[kind] = pair
    _EXTENSION_KINDS = found
    return found


def kinds() -> dict:
    """Every kind this install can carry out. Built-ins always win."""
    return {**_extension_kinds(), **KINDS}


def acting() -> int | None:
    """The approval id being carried out in this call stack, or None.

    For the layer underneath a performer. A function that writes to a service
    can call this and refuse when it is None, which turns "always go through
    red.py" from a convention the caller keeps into one the callee checks.
    """
    return _ACTING.get()


# --------------------------------------------------------------------------
# The flow
# --------------------------------------------------------------------------

def render(kind: str, payload: dict) -> str:
    known = kinds()
    if kind not in known:
        raise ValueError(f"unknown red action {kind!r}; known: {sorted(known)}")
    return known[kind][0](payload)


def request(kind: str, payload: dict, *, actor: str,
            thread_id: int | None = None) -> int:
    """File the approval and put it on the user's phone. Returns its id."""
    text = render(kind, payload)
    target = payload.get("to") if kind == "mail.send" else (
        payload.get("target") if kind not in KINDS else None)
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
    known = kinds()
    if kind not in known:
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
    token = _ACTING.set(approval_id)
    try:
        summary, ref = known[kind][1](payload)
    finally:
        _ACTING.reset(token)
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
