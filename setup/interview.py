"""Step 7: the interview. The part that makes this someone's agent.

An install with a connected mailbox and an empty `identity/` is a search engine
over your own life. It can tell you what is on Thursday; it cannot tell you
which of the four things on Thursday matters, because that depends entirely on
what you are trying to do and what you are like. Everything downstream --
which opportunities are worth an interruption, what leads the digest, when
silence is the right answer -- is ranked against files that do not exist until
somebody writes them.

So this asks for an hour of writing, and it is honest about why.

Four rounds:

  1. **The long piece.** One free-write, prompted rather than form-filled. The
     provocations are there to be ignored -- anyone who follows all fourteen in
     order produces a worse document than someone who answers three properly.
  2. **The follow-ups.** One model pass reads what they wrote and asks eight to
     fifteen questions *about that*, not from a template. This is where the
     detail that matters usually arrives: people write around the thing.
  3. **The hard facts.** A short form for what prose reliably misses -- an
     allergy, a medication, a person who must never be written down. Prose is
     the wrong shape for a rule that must never be got wrong.
  4. **Writing it up.** One more pass turns all of it into `identity/about.md`,
     `goals.md`, `preferences.md` and the personal half of the constitution.

The raw answers are kept alongside the written-up version, permanently. A later
session that disagrees with a conclusion in `about.md` can go and read what was
actually said, which is the difference between a document that can be corrected
and one that can only be overwritten.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

from herald import config, think

from .engine import DONE, PARTIAL, TODO, Field, Outcome, Prompt, State, Step

INTERVIEW_DIR = config.IDENTITY / "interview"
FILES = ("about.md", "goals.md", "preferences.md", "constitution.md")

PROVOCATIONS = """\
Write as much as you can stand. Aim for something between 800 and 2000 words —
long enough that it is actually about you rather than a summary of you. It does
not have to be organised, spell-checked, or fair to anyone. Nobody reads it but
your agent, and nothing in it leaves this machine.

Things worth covering, in any order, ignoring any that are not interesting:

- How you got to where you are. Not a CV — the turns that mattered.
- What you are actually trying to do over the next year, and by when.
- What you would be doing if the current thing stopped working out.
- What you are good at that people do not expect, and what you are bad at that
  they also do not expect.
- Who matters to you, and how you refer to them. (First names are fine — say
  who is who.)
- What a good week looks like, hour by hour, versus what a bad one looks like.
- What you procrastinate on, and what that costs you.
- Your money situation, in whatever detail you are comfortable with.
- Your health, sleep, and anything that reliably wrecks either.
- What you want to be told immediately, and what can always wait for morning.
- What you want an agent to *stop* you doing.
- What would make you switch this thing off within a week.
- Anything you would be annoyed to have to explain twice.
- Anything you are embarrassed to have written down but would rather it knew.

Take as long as you like. This is saved as you go — you can leave and come back.
"""

QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "8 to 15 follow-up questions, each about something "
                           "THIS person actually wrote or conspicuously did "
                           "not. Specific, answerable in a sentence or two, "
                           "and never a template question you would have asked "
                           "anyone.",
        },
        "read": {
            "type": "string",
            "description": "Three or four sentences: what you take from this so "
                           "far. Shown back to them, so second person, and "
                           "honest rather than flattering.",
        },
    },
    "required": ["questions", "read"],
}

QUESTION_PROMPT = """\
Someone has just written a long piece about themselves, to be read by a personal
agent that will act on their behalf every day.

Your job is to find what is missing. Read it closely and return follow-up
questions about *what they wrote*, not from a checklist. The good ones usually
come from:

- something stated as a fact whose consequence is unclear ("I have a job" — what
  hours, what does it cost you, is it the thing you care about?)
- an obvious tension between two things they said
- a person mentioned once and never explained
- something they clearly care about and described only in the abstract
- something almost everyone in their situation has to deal with that this piece
  does not mention at all
- a date or deadline implied but not stated

Do not ask anything answerable from what they already wrote. Do not ask more
than one question at a time. Do not psychoanalyse — you are collecting facts and
preferences, not interpreting them.

Here is what they wrote:

---

{piece}
"""

WRITEUP_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "What you wrote and what you are unsure about, in a "
                           "few sentences, addressed to them.",
        },
        "uncertain": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Things you had to guess at, phrased so they can "
                           "correct them. Empty if there are none.",
        },
    },
    "required": ["summary"],
}

WRITEUP_PROMPT = """\
You are setting up a new Herald for {name}. Everything below is what they told
it during setup: a long piece they wrote about themselves, their answers to
follow-up questions, and a short form of hard facts.

**Write the four identity files.** Use the Write tool. They are:

`ledger/identity/about.md` — who this person is. The arc that got them here, what
they are like, what the evidence supports. Third person; it is written for
whoever reads it next, including you tomorrow. Lead with what would change how
you treat them, not with a chronology. Quote their own phrasing where it is
distinctive — a person's own words about themselves are worth more than your
summary of them.

`ledger/identity/goals.md` — what they are chasing, with dates where they gave
any, ranked. Everything the agent ever surfaces is ranked against this file, so
vagueness here becomes noise in their notifications later. Include what would
count as a good outcome and what would count as a bad one. Note explicitly
anything they said they are *not* interested in: a rejection is as useful as a
preference and it is the half that usually goes unrecorded.

`ledger/identity/preferences.md` — how they want to be treated. Register, what
they want interrupted for, what can wait, what would make them stop using this.
Their words, not a personality summary.

`ledger/identity/constitution.md` — the personal half of the agent's operating
rules, read by every session alongside the shared constitution. This one is not
a description, it is instructions. It must contain, in this order:

1. Who the agent belongs to, and what it is called.
2. **The facts it must never have to look up** — allergies, medical conditions,
   safety constraints, anything where being wrong once is unacceptable. State
   each one plainly and say what it means in practice. If they gave none, say
   that there are none rather than leaving the section out.
3. How they want to be spoken to, in a form an agent can follow.
4. Anything their institution, employer or circumstances forbid — an academic
   integrity policy, an NDA, a compliance rule. Cite the source if they gave one.
5. Anyone or anything that must never be written down or acted on.

Rules for all four:

- **Do not invent.** Everything must trace to something they said. Where you are
  extrapolating, say so in the file itself.
- **Do not flatter.** A file that reads like an admiring profile is useless to
  the agent that has to give this person real advice.
- **Two facts in one sentence become one fact later.** Keep unrelated facts in
  unrelated sentences.
- **Do not psychoanalyse.** Record what they said and what follows from it.
- If they said nothing about an area, the file says nothing about it. An honest
  gap is better than a confident guess, and the gap gets filled later.

Then return the schema: a short summary addressed to them, and a list of
anything you had to guess at so they can correct it.

---

## The long piece

{piece}

## Follow-up questions and their answers

{answers}

## Hard facts, from the form

{facts}

## What Herald already knows

Name: {name}. Pronouns: {pronouns}. Timezone: {timezone}.
Agent name: {agent}.
"""


def _dir() -> pathlib.Path:
    INTERVIEW_DIR.mkdir(parents=True, exist_ok=True)
    return INTERVIEW_DIR


def _piece_path() -> pathlib.Path:
    return _dir() / "piece.md"


def _answers_path() -> pathlib.Path:
    return _dir() / "answers.json"


def _draft_path() -> pathlib.Path:
    return _dir() / "draft.md"


def _current_text() -> str:
    """What to show in the box: the saved piece, else the autosaved draft.

    The browser writes `draft.md` as the person types (setup/web/server.py),
    which is what makes "you can leave and come back" true -- but only if the
    field actually reads it back. It did not, the first time.
    """
    if _piece_path().exists() and _piece_path().read_text().strip():
        return _piece_path().read_text()
    if _draft_path().exists():
        return _draft_path().read_text()
    return ""


def _stage(state: State) -> str:
    step = state.step("interview")
    if not _piece_path().exists() or not _piece_path().read_text().strip():
        return "write"
    if not step.get("questions"):
        return "ask"
    if not step.get("answered"):
        return "answer"
    if not step.get("facts_done"):
        return "facts"
    if not all((config.IDENTITY / f).exists() for f in FILES):
        return "writeup"
    return "done"


def status(state: State) -> tuple[str, str]:
    written = [f for f in FILES if (config.IDENTITY / f).exists()]
    if len(written) == len(FILES):
        if _piece_path().exists():
            words = len(_piece_path().read_text().split())
            return DONE, f"{len(written)} identity files, from {words} words you wrote"
        return DONE, f"{len(written)} identity files (written outside the wizard)"
    if _piece_path().exists() and _piece_path().read_text().strip():
        return PARTIAL, f"started — at the {_stage(state)} stage"
    return TODO, "your agent does not know who you are yet"


def prompt(state: State) -> Prompt:
    stage = _stage(state)
    step = state.step("interview")

    if stage == "write":
        return Prompt(
            title="Tell it who you are",
            blurb="This is the part that makes the difference between an agent "
                  "that can search your calendar and one that can tell you which "
                  "thing on it matters.\n\n" + PROVOCATIONS,
            fields=[Field(key="piece", label="", type="textarea", rows=30,
                          dictate=True, required=True,
                          default=_current_text(),
                          help="Type or dictate. Saved as you go in the browser, "
                               "and for good when you press the button.")],
            action="Save and read it")

    if stage == "ask":
        return Prompt(
            title="Reading what you wrote",
            blurb="One model pass, on your own subscription, to work out what to "
                  "ask you next. It takes half a minute.",
            fields=[], immediate=True, action="Go ahead")

    if stage == "answer":
        fields = [Field(key="read", type="note", label="What it took from that",
                        help=step.get("read", ""))]
        for i, q in enumerate(step.get("questions", [])):
            fields.append(Field(key=f"q{i}", label=q, type="textarea", rows=3,
                                dictate=True,
                                help="Skip any that are not worth answering."))
        return Prompt(
            title="A few things it wants to know",
            blurb="These come from what you wrote rather than from a list. "
                  "Answer the ones worth answering; leave the rest blank.",
            fields=fields, action="Save these answers")

    if stage == "facts":
        return Prompt(
            title="The things it must never get wrong",
            blurb="Prose is the wrong shape for a rule that has to hold every "
                  "time. These go into your agent's operating rules, read by "
                  "every session, and it is told never to have to look them up.",
            fields=[
                Field(key="critical", label="Anything where being wrong once is "
                                            "unacceptable", type="textarea", rows=4,
                      placeholder="allergies, medical conditions, safety "
                                  "constraints, a court order, a diet that is "
                                  "not a preference",
                      help="Say what it is and what it means in practice. Leave "
                           "blank if there is genuinely nothing."),
                Field(key="forbidden", label="Anything your work, school or "
                                             "circumstances forbid",
                      type="textarea", rows=3,
                      placeholder="an academic integrity policy, an NDA, a "
                                  "compliance rule, code you may not share",
                      help="Quote the rule and where it comes from if you can — "
                           "an approximate rule is one nobody can check."),
                Field(key="never_record", label="Anyone or anything that must "
                                                "never be written down",
                      type="textarea", rows=3,
                      help="People who did not agree to be in a database, "
                           "subjects you do not want kept."),
                Field(key="interrupt", label="What is always worth interrupting "
                                             "you for", type="textarea", rows=2,
                      placeholder="a deadline inside 48 hours; anything from my "
                                  "manager; nothing, ever"),
                Field(key="quiet", label="When not to", type="text",
                      placeholder="after 22:00; during work hours; no quiet hours",
                      help="Herald has no way to know this and will otherwise "
                           "assume any time is fine."),
            ],
            action="Save and write it up")

    if stage == "writeup":
        return Prompt(
            title="Writing it up",
            blurb="One more pass — the important one — turning all of that into "
                  "the four files your agent reads. This one uses the stronger "
                  "model and takes a couple of minutes.\n\nYou get to read and "
                  "edit everything it writes in the next step.",
            fields=[], immediate=True, action="Write it")

    words = len(_piece_path().read_text().split())
    return Prompt(
        title="It knows who you are",
        blurb=f"Written from {words} words of yours, kept at "
              f"`ledger/identity/interview/`. Running this step again starts a "
              f"new interview rather than editing the old one — to change "
              f"something, edit the files directly or just tell your agent.",
        fields=[], immediate=True, action="Start a new interview")


def apply(state: State, answers: dict) -> Outcome:
    stage = _stage(state)
    step = state.step("interview")

    if stage == "write" or answers.get("piece"):
        piece = (answers.get("piece") or "").strip()
        if len(piece.split()) < 50:
            return Outcome(ok=False,
                           message="That is too short to be worth a model pass.",
                           detail="Fifty words is the floor and eight hundred is "
                                  "the point. If you would rather do this later, "
                                  "skip the step — everything else works without "
                                  "it, just worse.")
        _piece_path().write_text(piece)
        if _draft_path().exists():
            _draft_path().unlink()
        step["words"] = len(piece.split())
        step.pop("questions", None)
        step.pop("answered", None)
        return Outcome(ok=True, message=f"Saved {step['words']} words.", more=True)

    if stage == "ask":
        piece = _piece_path().read_text()
        result = think.think(QUESTION_PROMPT.format(piece=piece),
                             label="setup:interview-questions",
                             cwd=config.ROOT, json_schema=QUESTION_SCHEMA,
                             allowed_tools=[], permission_mode="auto")
        if not result.ok:
            return Outcome(ok=False, message="That pass failed.",
                           detail=result.error or "")
        payload = result.json() or {}
        step["questions"] = payload.get("questions", [])[:15]
        step["read"] = payload.get("read", "")
        if not step["questions"]:
            step["answered"] = True
        return Outcome(ok=True, message=step.get("read") or "Read it.", more=True)

    if stage == "answer":
        pairs = []
        for i, q in enumerate(step.get("questions", [])):
            a = (answers.get(f"q{i}") or "").strip()
            if a:
                pairs.append({"q": q, "a": a})
        step["answers"] = pairs
        step["answered"] = True
        _answers_path().write_text(json.dumps(pairs, indent=2))
        return Outcome(ok=True,
                       message=f"{len(pairs)} of {len(step.get('questions', []))} "
                               f"answered.", more=True)

    if stage == "facts":
        facts = {k: (answers.get(k) or "").strip()
                 for k in ("critical", "forbidden", "never_record", "interrupt", "quiet")}
        step["facts"] = facts
        step["facts_done"] = True
        (_dir() / "facts.json").write_text(json.dumps(facts, indent=2))
        return Outcome(ok=True, message="Saved. Now the write-up.", more=True)

    if stage == "writeup":
        who = config.person()
        pairs = step.get("answers", [])
        facts = step.get("facts", {})
        prompt_text = WRITEUP_PROMPT.format(
            name=who["name"] or "the user",
            pronouns=config.get("user.pronouns", "they/them"),
            timezone=config.get("timezone", "UTC"),
            agent=config.get("agent.name", "Herald"),
            piece=_piece_path().read_text(),
            answers="\n\n".join(f"**{p['q']}**\n\n{p['a']}" for p in pairs)
                    or "(they skipped the follow-ups)",
            facts="\n".join(f"- **{k}**: {v}" for k, v in facts.items() if v)
                  or "(nothing given)",
        )
        result = think.think(prompt_text, label="setup:interview-writeup",
                             cwd=config.ROOT, json_schema=WRITEUP_SCHEMA,
                             escalate=True, permission_mode="auto",
                             allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"])
        if not result.ok:
            return Outcome(ok=False, message="The write-up failed.",
                           detail=(result.error or "")[:500])
        missing = [f for f in FILES if not (config.IDENTITY / f).exists()]
        if missing:
            return Outcome(ok=False,
                           message=f"It did not write: {', '.join(missing)}",
                           detail="Run this step again; nothing you typed is lost.")
        payload = result.json() or {}
        step["summary"] = payload.get("summary", "")
        step["uncertain"] = payload.get("uncertain", [])
        (_dir() / f"{dt.date.today():%Y-%m-%d}-writeup.json").write_text(
            json.dumps(payload, indent=2))
        # The rules file changed, so the file every session reads has to be
        # rebuilt from it.
        from herald import constitution  # noqa: PLC0415
        constitution.write()
        return Outcome(ok=True, message=payload.get("summary", "Written."),
                       warnings=payload.get("uncertain", []))

    # done -> start over
    step.clear()
    if _piece_path().exists():
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
        _piece_path().rename(_dir() / f"piece-{stamp}.md")
    return Outcome(ok=True, message="Starting a new interview.", more=True)


STEP = Step(key="interview", title="Tell it who you are", optional=True,
            summary="An hour of writing that everything else is ranked against",
            status_fn=status, prompt_fn=prompt, apply_fn=apply)
