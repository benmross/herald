"""Step 5: Telegram, which is how the agent reaches a person who is not at a desk.

Optional, and the wizard says so, but an agent that can only be reached by
sitting at the machine it runs on is a different and much smaller thing. This is
the surface the morning digest arrives on, the one a deadline interrupts on, and
the one where "yes, do that" is a tap.

BotFather is the only part that cannot be automated: Telegram will not create a
bot without a person talking to another bot. Everything after that -- learning
which chat to send to, checking the token is real, offering the forum-topic
layout -- happens here.
"""

from __future__ import annotations

import json
import secrets as secrets_mod
import string
import urllib.error
import urllib.request

from herald import config

from .engine import DONE, TODO, Field, Outcome, Prompt, State, Step

BOTFATHER = """\
**1.** Open Telegram and message [@BotFather](https://t.me/BotFather) — it is
Telegram's own bot for making bots.

**2.** Send it `/newbot`. It asks for a display name (anything: `{agent}`) and
then a username, which must be unique and end in `bot` — something like
`{suggestion}`.

**3.** It replies with a line like
`Use this token to access the HTTP API: 8000123456:AAF-xxxxxxxxxxxxxxxxxxxx`.
Copy that whole token and paste it below.

That token is a password for the bot: anyone holding it can read and send its
messages. Herald keeps it in `secrets.json` at mode 600 and nowhere else.
"""

SAY_HELLO = """\
The bot exists. Now it has to learn where to send things.

**Open Telegram, find your bot ({handle}), and send it any message** — "hello"
is fine. Then press the button below.

Telegram will not let a bot message someone who has never messaged it first,
which is a good rule and the reason for this step.
"""


def _api(token: str, method: str, **params):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        try:
            return json.load(exc)
        except Exception:                                           # noqa: BLE001
            return {"ok": False, "description": f"HTTP {exc.code}"}
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "description": str(exc)}


def _stage(state: State) -> str:
    if not config.secret("telegram.bot_token"):
        return "token"
    if not config.secret("telegram.chat_id"):
        return "hello"
    return "done"


def status(state: State) -> tuple[str, str]:
    token = config.secret("telegram.bot_token")
    if not token:
        return TODO, "not set up — Herald can still be used from a terminal"
    if not config.secret("telegram.chat_id"):
        return TODO, "bot created, but it does not know where to send yet"
    handle = state.step("telegram").get("handle", "your bot")
    return DONE, f"@{handle} → chat {config.secret('telegram.chat_id')}"


def prompt(state: State) -> Prompt:
    stage = _stage(state)
    if stage == "token":
        suffix = "".join(secrets_mod.choice(string.ascii_lowercase) for _ in range(4))
        agent = config.get("agent.name", "Herald")
        return Prompt(
            title="Telegram (recommended)",
            blurb="How your agent reaches you when you are not at this machine, "
                  "and how you answer it.\n\n"
                  + BOTFATHER.format(agent=agent,
                                     suggestion=f"{agent.lower()}_{suffix}_bot"),
            fields=[Field(key="token", label="The bot token", type="secret",
                          required=True,
                          placeholder="8000123456:AAF-xxxxxxxxxxxxxxxxxxxxxxxx")],
            action="Check the token")
    if stage == "hello":
        handle = state.step("telegram").get("handle", "your bot")
        return Prompt(title="Say hello to your bot",
                      blurb=SAY_HELLO.format(handle=f"@{handle}"),
                      fields=[], immediate=True, action="I've sent it a message")
    handle = state.step("telegram").get("handle", "your bot")
    return Prompt(
        title="Telegram is connected",
        blurb=f"@{handle} will carry your digests and anything urgent, and you "
              f"can talk back to it in the same thread.\n\nRunning this step "
              f"again starts over with a new token, which is what to do if you "
              f"ever regenerate one.",
        fields=[], immediate=True, action="Start over")


def apply(state: State, answers: dict) -> Outcome:
    stage = _stage(state)

    if stage == "token" or answers.get("token"):
        token = (answers.get("token") or "").strip()
        if not token:
            return Outcome(ok=False, message="No token pasted.")
        if ":" not in token:
            return Outcome(ok=False, message="That does not look like a bot token.",
                           detail="It should be a number, a colon, then a long "
                                  "string — BotFather sends it on its own line.")
        me = _api(token, "getMe")
        if not me.get("ok"):
            return Outcome(ok=False, message="Telegram rejected that token.",
                           detail=me.get("description", "")
                                  + " Check you copied all of it, including the "
                                    "part before the colon.")
        handle = me["result"].get("username", "")
        config.set_secret("telegram.bot_token", token)
        state.step("telegram")["handle"] = handle
        config.set_user("capabilities.telegram", True)
        return Outcome(ok=True, message=f"That is @{handle}. Now message it.",
                       more=True)

    # "I've sent it a message": learn the chat and the sender.
    token = config.secret("telegram.bot_token")
    updates = _api(token, "getUpdates", timeout=0, limit=20)
    if not updates.get("ok"):
        return Outcome(ok=False, message="Could not reach Telegram.",
                       detail=updates.get("description", ""))
    results = updates.get("result") or []
    messages = [u.get("message") for u in results if u.get("message")]
    if not messages:
        return Outcome(
            ok=False, message="Telegram has not seen a message to your bot yet.",
            detail="Open Telegram, find the bot, and send it anything — then "
                   "press the button again. If the Herald bridge is already "
                   "running, stop it first (`systemctl --user stop "
                   "herald-telegram`): Telegram only allows one reader at a time.")
    last = messages[-1]
    chat_id = (last.get("chat") or {}).get("id")
    user_id = (last.get("from") or {}).get("id")
    first = (last.get("from") or {}).get("first_name", "")
    config.set_secret("telegram.chat_id", chat_id)
    config.set_secret("telegram.user_id", user_id)

    sent = _api(token, "sendMessage", chat_id=chat_id,
                text=f"{config.get('agent.name', 'Herald')} is connected. "
                     f"This is where your digests will land.")
    warnings = []
    if not sent.get("ok"):
        warnings.append(f"could not send a test message: {sent.get('description')}")
    return Outcome(
        ok=True,
        message=f"Connected to {first or 'you'}. A test message just went to "
                f"that chat.",
        warnings=warnings,
        detail="Only messages from your own Telegram account are answered; "
               "anyone else who finds the bot gets nothing.")


STEP = Step(key="telegram", title="Telegram", optional=True,
            summary="How your agent reaches you, and how you answer it",
            status_fn=status, prompt_fn=prompt, apply_fn=apply)
