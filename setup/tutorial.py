"""Last step: an animated tour of Telegram, topics and the commands.

The command guide pinned in Telegram says what each command does. This shows
how they are meant to be used together: one topic per project, /new for a
fresh start that still sees the last few messages, /clear for a clean slate,
a reply to go back, /models for speed against depth,
approvals as buttons. The web page plays it; a terminal gets the guide text.

The bot's lines come from `herald.topicstatus`, the module the real bridge
answers with, so the tour cannot drift from what the bot actually says. The
examples are invented and contain nothing about the person setting up.
"""

from __future__ import annotations

from herald import config, topicstatus

from .engine import DONE, Field, Outcome, Prompt, State, Step

OPUS = {"engine": "claude", "model": "claude-opus-5-5", "effort": "high"}
SONNET = {"engine": "claude", "model": "claude-sonnet-5-5", "effort": "high"}
GPT = {"engine": "codex", "model": "gpt-6.1-sol", "effort": "medium"}


def status(state: State) -> tuple[str, str]:
    return (DONE, "") if state.step("tutorial").get("seen") else ("todo", "")


def me(text: str, **kw) -> dict:
    return {"who": "me", "text": text, **kw}


def bot(text: str, **kw) -> dict:
    return {"who": "bot", "text": text, **kw}


def event(text: str, **kw) -> dict:
    return {"who": "event", "text": text, **kw}


def scenes() -> list[dict]:
    agent = config.get("agent.name", "Herald")
    engines = [e for e in ("claude", "codex", "kiro") if e in config.enabled_engines()]
    topics = ["General", "Website", "Spring break"]
    old_site = [me("The contact form on my site doesn't send anything", old=True),
                bot("Found it: the form posts to an old address. I fixed it and "
                    "sent a test message, which arrived.", old=True)]
    old_trip = [me("Find flights to Denver, March 13 to 21", old=True, id="flights"),
                bot("Three under $300. The cheapest is $214 on United, leaving "
                    "at 7:05 AM. Want it on your calendar?", old=True)]
    out = [
        {"title": "One topic per project",
         "caption": "Each topic is its own conversation. Give each project or "
                    "errand a topic so they never get mixed together. General is "
                    "for quick questions, and it starts fresh after a few quiet hours.",
         "topics": topics[:2], "topic": "Spring break",
         "steps": [event("You created the topic “Spring break”", new_topic="Spring break"),
                   me("Find flights to Denver, March 13 to 21"),
                   bot("Three under $300. The cheapest is $214 on United, leaving "
                       "at 7:05 AM. Want it on your calendar?")]},
        {"title": "Come back any time",
         "caption": "A topic picks up where it left off, even days later. "
                    f"{agent} also remembers you through its notes, so every "
                    "conversation already knows your schedule and preferences.",
         "topics": topics, "topic": "Website", "history": old_site,
         "steps": [me("Can you add the dark mode toggle too?"),
                   bot("Done. It's in the footer and remembers the choice. "
                       "The contact form fix from earlier is still live.")]},
        {"title": "See what it's using",
         "caption": "/context shows the model, the thinking level and how full "
                    "this conversation is. A conversation that fills up continues "
                    "on its own as a new one, with a summary of the old.",
         "topics": topics, "topic": "Website", "history": old_site,
         "steps": [me("/context"),
                   bot(topicstatus.now(OPUS) + "\n"
                       "Conversation: 4 messages, started 2 days ago, $0.84 so far\n"
                       "Context: 61,240 of 1,000,000 tokens (6%). At 750,000 (75%) "
                       "the next message starts a new conversation with a summary "
                       "of this one.\n"
                       "Last message: 38.0s, 12 steps; 310 new tokens in, 58,900 "
                       "reread from cache, 1,420 out", mono=True)]},
        {"title": "/new keeps the last few messages",
         "caption": "/new starts a fresh conversation that still sees the last "
                    "few messages of the old one, so you can refer back to them. "
                    "Use it when a long conversation moves on to its next step. "
                    "The model and thinking level stay.",
         "topics": topics, "topic": "Spring break", "history": old_trip,
         "steps": [me("/new"),
                   bot(topicstatus.new_note(OPUS, True, kept=True)),
                   me("What should I pack for that trip?"),
                   bot("For Denver in March: layers. Highs around 55°F and lows "
                       "near freezing, and snow is possible. A packing list is below.")]},
        {"title": "/clear forgets it all",
         "caption": "/clear starts over with nothing from the old conversation. "
                    "Use it when you move on to something unrelated in the same "
                    "topic. /reset does the same. Either way, replying to an older "
                    "message still brings that conversation back.",
         "topics": topics, "topic": "Website", "history": old_site,
         "steps": [me("/clear"),
                   bot(topicstatus.new_note(OPUS, True)),
                   me("Write alt text for the photos on my About page"),
                   bot("Done. All six photos now have alt text describing what's in them.")]},
        {"title": "Reply to go back",
         "caption": "Replying to an older message returns to the conversation it "
                    "came from, even after /new. The messages after that continue it.",
         "topics": topics, "topic": "Spring break",
         "history": old_trip + [me("/new", old=True),
                                bot(topicstatus.new_note(OPUS, True, kept=True), old=True),
                                me("What should I pack for that trip?", old=True),
                                bot("Layers. Highs around 55°F and lows near freezing.", old=True)],
         "steps": [me("Put the $214 one on my calendar", reply_to="flights"),
                   event("Back in the conversation from 14 Nov, 3:12 PM."),
                   bot("Added: United to Denver, Friday March 13 at 7:05 AM.")]},
        {"title": "Faster or deeper",
         "caption": "/models picks the model and thinking level. Lighter models "
                    "and lower thinking answer faster and use less of your plan. "
                    "Higher thinking is for hard problems. Changing either on the "
                    "same provider keeps the conversation.",
         "topics": topics, "topic": "Website", "history": old_site,
         "steps": [me("/models"),
                   bot(topicstatus.now(OPUS) + "\nTap a model to switch.",
                       buttons=[["Claude Opus 5.5", "Claude Sonnet 5.5"], ["GPT-6.1 Sol"]],
                       tap="Claude Sonnet 5.5"),
                   bot(topicstatus.same_conversation_note(SONNET, "Model set to Claude Sonnet 5.5.")
                       + "\n\nChoose a thinking level for Claude Sonnet 5.5. Current: high.",
                       buttons=[["Default", "low", "medium"], ["high", "max"]], tap="low",
                       edit=True),
                   bot(topicstatus.same_conversation_note(
                       {**SONNET, "effort": "low"}, "Thinking set to low."), edit=True)]},
    ]
    if len(engines) > 1:
        other = "codex" if "codex" in engines else engines[1]
        target = GPT if other == "codex" else {"engine": other, "model": None, "effort": None}
        out.append(
            {"title": "Switching provider",
             "caption": f"/{other} and /claude move a topic to another provider. "
                        "It can't read the old provider's conversation, so a new one "
                        "starts with a summary of what the topic was working on.",
             "topics": topics, "topic": "Website", "history": old_site,
             "steps": [me(f"/{other}"),
                       bot(topicstatus.switch_note(target, "claude", True))]})
    out += [
        {"title": "Stop it mid-task",
         "caption": "While it works you see what it's doing. /stop ends the task "
                    "straight away, and a new message after that starts fresh.",
         "topics": topics, "topic": "General",
         "steps": [me("Reorganize every folder in my Drive by year"),
                   bot("thinking...\nlisting Drive folders\nreading 412 files",
                       mono=True, progress=True),
                   me("/stop"),
                   bot("Stopped. That task is abandoned.", italic=True)]},
        {"title": "It asks before anything others see",
         "caption": "Emails, posts, sign-ups and purchases wait for a tap. "
                    "Ignoring the question never counts as yes.",
         "topics": topics, "topic": "General",
         "steps": [me("Email Dana that I'll be 10 minutes late"),
                   bot("Approve this action?\n\nSend an email to Dana Ortiz:\n"
                       "“Running about 10 minutes late, see you soon.”",
                       buttons=[["Approve", "Decline"]], tap="Approve"),
                   bot("Sent to Dana Ortiz.")]},
        {"title": "Private conversations",
         "caption": "/private starts a conversation where nothing is saved: it can "
                    "read, but it can't write notes or change anything. "
                    "/private off ends it and deletes the transcript.",
         "topics": topics, "topic": "General",
         "steps": [me("/private"),
                   bot("Private from here in this topic. I can read everything and "
                       "can save or change nothing, and no record of this "
                       "conversation is kept."),
                   me("/private off"),
                   bot("Private session ended and its transcript deleted.")]},
        {"title": "The guide stays pinned",
         "caption": "The guide pinned at the top of the chat lists every command. "
                    "/help sends it again whenever you need it.",
         "topics": topics, "topic": "General",
         "steps": [me("/help"),
                   bot(topicstatus.guide(), pin=True)]},
    ]
    return out


def prompt(state: State) -> Prompt:
    telegram = bool(config.secret("telegram.chat_id"))
    blurb = ("A tour of how topics and commands are meant to be used. It plays "
             "on its own; use the arrows or the scene list to move around.")
    if not telegram:
        blurb += (" Telegram is not connected yet. `herald setup --step telegram` "
                  "connects it in about five minutes.")
    return Prompt(title="Using Telegram", blurb=blurb, action="Finish",
                  fields=[Field(key="tour", label="", type="tutorial",
                                help=topicstatus.guide(), data=scenes())])


def apply(state: State, answers: dict) -> Outcome:
    state.step("tutorial")["seen"] = True
    return Outcome(ok=True, message="That's everything. /help in Telegram brings "
                                    "the command guide back.")


STEP = Step(key="tutorial", title="Using Telegram", optional=True,
            summary="Topics and commands, shown",
            status_fn=status, prompt_fn=prompt, apply_fn=apply)
