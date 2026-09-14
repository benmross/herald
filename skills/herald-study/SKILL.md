---
name: herald-study
description: Teach and quiz the user on any subject through Telegram quizzes that run without a model call per answer. Use when they ask to be quizzed, tested, drilled or reviewed on a course or topic, want practice before an exam or quiz, send practice material and ask for questions from it, or when a message begins "[The quiz engine wrote this message" (a finished quiz to assess, or one answer to check). Also use before offering a quiz. Never start one they did not ask for; offering is fine.
---

# Quizzes

A quiz is one JSON set, written once by a session and delivered by the
Telegram bridge. Taps and typed answers in that topic are checked by
`lib/herald/study.py` against what the set already holds, so answering costs no
model round trip. When the set ends, the bridge sends this topic's session the
transcript in one turn to assess. Read the module's docstring for the design.

**Offer, do not start.** The user can always ask for a quiz. Offering one
when an exam or quiz is close is welcome. Starting one they did not ask for is
not.

## Writing a set

Default to 5 to 8 items unless they ask for a different length, and use that
length until they change it again. Quick review is the point: prefer `choice`
and `short`, which are checked instantly, and use `open` only where the skill
being tested really is writing something (a proof, a simplification with
labelled steps).

Before writing one for a course, read `ledger/state/areas/<AREA>.md`: its
material on file (build from the real slides and practice papers when they
exist) and **its academic-integrity rules**. Quizzing on concepts is study.
Anything the constitution or the area file says Herald may not touch (graded
work, project code) stays untouched, and a quiz is never built from a problem
that is currently assigned for credit.

```json
{
  "title": "Exam 1 practice",
  "area": "CMSC250",
  "intro": "optional one line",
  "items": [
    {"kind": "choice", "topic": "conditionals",
     "prompt": "Which is the contrapositive of p → q?",
     "options": ["q → p", "¬p → ¬q", "¬q → ¬p"],
     "answer": 2,
     "feedback": ["That's the converse.", "That's the inverse.", "Yes: swap and negate both."],
     "solution": "optional, shown after a wrong answer"},
    {"kind": "short", "topic": "binary",
     "prompt": "Convert (1101011)₂ to decimal.",
     "accept": ["107"], "display": "107",
     "feedback_wrong": "optional", "solution": "64 + 32 + 8 + 2 + 1 = 107"},
    {"kind": "open", "topic": "simplification",
     "prompt": "Simplify ¬(p → q) ∨ q, naming each law.",
     "solution": "the worked answer in the format the course wants"}
  ]
}
```

- `short.accept` lists every form that should count. Comparison ignores case,
  spaces and `, ; : .`, maps true/false to t/f, and maps and/or/not, `^ & | ~ !
  ->` and a spaced `v` onto ∧ ∨ ¬ →. So `["FTFTT", "A=F B=T C=F D=T E=T"]` covers
  most ways of typing five truth values. Say in the prompt what shape to reply in.
- Text is Markdown, converted by `tgtext`. **Telegram cannot render LaTeX.**
  Use Unicode (¬ ∧ ∨ → ↔ ⊕ ∀ ∃ ∈ ⊆ ∪ ∩ ≡ ₂ ², fractions as a/b) and put
  truth tables, matrices and anything aligned in a fenced code block.
- Write solutions in the form the course grades, from its style guide if one is
  on file. The solution is what they learn from, so it is worth the care.

Then:

```bash
herald quiz start /tmp/set.json          # validates, sends Q1 to this topic
herald quiz start /tmp/set.json --topic <chat_id>:<thread_id|main>
herald quiz status                       # recent sets and scores
herald quiz stop                         # end this topic's live set, no review
```

With no `--topic` it goes to the topic whose turn is running, which is the one
that asked. Say so briefly in the reply; the questions arrive as their own
messages.

In the chat: `/skip` skips, `/endquiz` ends early. While the quiz waits on a
rating button, typed messages go to the session as usual.

## Assessing a finished set

A turn beginning "[The quiz engine wrote this message" carries every question,
its solution, their answer, the automatic check and their self-rating.

1. Grade each written answer against the solution as the course would. Their
   self-rating is a first pass; correct it where it was generous or harsh, and
   say which.
2. Record what they know in `ledger/state/study/<area>.md` (create it): per
   topic, solid, shaky or missed, dated, with the specific mistake. This is what
   the next set is built from, so update it rather than appending a transcript.
3. Reply briefly: what was wrong and exactly why, and what to review. Offer the
   next set, aimed at what was missed.

A "check this one now" turn is a single answer mid-quiz: a few sentences on
whether it earns full credit and what is missing, nothing else.
