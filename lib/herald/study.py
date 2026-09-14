"""Quizzes on Telegram, answered without a model in the loop.

Asked for on 14 Sep 2026: the user wanted to be taught and quizzed on their
courses, and wanted it to cost as few model round trips as possible. A turn is
a median of nine round trips, so a quiz that invoked a session for every answer
would spend a minute of wall clock per question just to say "correct".

So the model runs twice per set, not once per answer:

1. A session writes the whole set up front as JSON -- the questions, what
   counts as right, feedback per multiple-choice option, and a worked solution
   for everything. `herald quiz start <file>` loads it and sends question one.
2. The bridge delivers it. A tap or a typed reply in that topic is caught here
   before any session starts, checked deterministically where it can be, and
   answered with the feedback already written.
3. When the set ends, the bridge hands the whole transcript to the topic's
   session in one turn: grade the freeform answers against their solutions,
   record what they know, say what to do next.

Three item kinds, because most real exam questions are not multiple choice:

- `choice`: tap a letter, get that option's own feedback.
- `short`: a typed answer with a small set of accepted forms (a number, a
  truth-table column, "A=F B=T"), normalised and compared.
- `open`: a proof or a simplification. Type an attempt (or don't), see the
  worked solution at once, and rate yourself. The rating is only a first
  pass; the end-of-set turn reads what was actually written and corrects it.
  "Check this one now" hands that single answer to a session, for when
  waiting for the end is not good enough.

Quick review is the priority: nothing here ever waits on a model unless the
user asked it to.
"""

from __future__ import annotations

import html as _html
import json
import re
import sqlite3
import unicodedata
from typing import Callable

from herald import db, tgtext

KINDS = ("choice", "short", "open")
LETTERS = "ABCDEFGH"
RATINGS = {"g": "got it", "p": "partial", "m": "missed"}

# The bridge's `api(method, **params) -> dict | None`, injected so the same
# code runs from the CLI, the bridge and the tests.
Api = Callable[..., "dict | None"]


class SetError(ValueError):
    pass


# -- loading ------------------------------------------------------------------

def validate(spec: dict) -> dict:
    """Refuse a malformed set before anything reaches a phone. A bad item
    discovered at question five strands the quiz halfway through."""
    if not isinstance(spec, dict) or not spec.get("items"):
        raise SetError("a set needs a non-empty `items` list")
    for n, item in enumerate(spec["items"], 1):
        kind = item.get("kind")
        if kind not in KINDS:
            raise SetError(f"item {n}: kind must be one of {KINDS}, not {kind!r}")
        if not str(item.get("prompt") or "").strip():
            raise SetError(f"item {n}: empty prompt")
        if kind == "choice":
            opts = item.get("options") or []
            if not 2 <= len(opts) <= len(LETTERS):
                raise SetError(f"item {n}: choice needs 2-{len(LETTERS)} options")
            if not isinstance(item.get("answer"), int) or not 0 <= item["answer"] < len(opts):
                raise SetError(f"item {n}: `answer` must index into options")
            fb = item.get("feedback")
            if fb is not None and len(fb) != len(opts):
                raise SetError(f"item {n}: `feedback` needs one entry per option")
        if kind == "short" and not item.get("accept"):
            raise SetError(f"item {n}: short needs an `accept` list")
        if kind == "open" and not str(item.get("solution") or "").strip():
            raise SetError(f"item {n}: open needs a `solution`")
    return spec


def create(con: sqlite3.Connection, spec: dict, chat_id: int,
           thread_id: int | None) -> int:
    validate(spec)
    # One live set per topic: a typed answer has to mean exactly one thing.
    con.execute("UPDATE study_sets SET state = 'abandoned', finished = ? "
                "WHERE chat_id = ? AND IFNULL(thread_id, 0) = ? AND state = 'active'",
                (db.now(), chat_id, thread_id or 0))
    cur = con.execute(
        "INSERT INTO study_sets (created, chat_id, thread_id, title, area, spec, "
        "state, pos, awaiting) VALUES (?, ?, ?, ?, ?, ?, 'active', 0, 'answer')",
        (db.now(), chat_id, thread_id, spec.get("title") or "Quiz",
         spec.get("area"), json.dumps(spec)))
    con.commit()
    return cur.lastrowid


def active_for(con: sqlite3.Connection, chat_id: int,
               thread_id: int | None) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM study_sets WHERE chat_id = ? AND IFNULL(thread_id, 0) = ? "
        "AND state = 'active' ORDER BY id DESC LIMIT 1",
        (chat_id, thread_id or 0)).fetchone()


# -- checking -----------------------------------------------------------------

_DROP = re.compile(r"[\s,;:.`'\"]+")


def normalise(text: str) -> str:
    """Forgiving about typing on a phone, strict about content.

    Case, spacing and separators go; `true`/`false` become `t`/`f`, and the
    logic symbols a phone keyboard lacks map onto the ones it has, so
    "p or q", "p v q" and "p∨q" all compare equal.
    """
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = re.sub(r"\btrue\b", "t", t)
    t = re.sub(r"\bfalse\b", "f", t)
    for word, sym in (("and", "∧"), ("or", "∨"), ("not", "¬"), ("xor", "⊕")):
        t = re.sub(rf"\b{word}\b", sym, t)
    t = (t.replace("^", "∧").replace("&", "∧").replace("|", "∨").replace("~", "¬")
          .replace("!", "¬").replace("->", "→").replace("<->", "↔"))
    t = re.sub(r"(?<=[\s)a-z])v(?=[\s(¬a-z])", "∨", t)
    return _DROP.sub("", t)


def is_correct(item: dict, answer: str) -> bool:
    got = normalise(answer)
    return any(got == normalise(a) for a in item.get("accept") or [])


# -- rendering ----------------------------------------------------------------

def _kb(*rows) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row]
                                for row in rows if row]}


def question(row: sqlite3.Row, idx: int) -> tuple[str, dict]:
    spec = json.loads(row["spec"])
    item = spec["items"][idx]
    sid, total = row["id"], len(spec["items"])
    head = f"**Q{idx + 1}/{total}**" + (f"  _{item['topic']}_" if item.get("topic") else "")
    body = [head, "", item["prompt"]]
    tail = [("Skip", f"study:{sid}:{idx}:s"), ("End quiz", f"study:{sid}:x")]
    if item["kind"] == "choice":
        body += [""] + [f"{LETTERS[i]}. {o}" for i, o in enumerate(item["options"])]
        letters = [(LETTERS[i], f"study:{sid}:{idx}:c:{i}")
                   for i in range(len(item["options"]))]
        return tgtext.to_html("\n".join(body)), _kb(letters, tail)
    if item["kind"] == "short":
        body += ["", "_Reply with your answer._"]
        return tgtext.to_html("\n".join(body)), _kb(tail)
    body += ["", "_Reply with your attempt, or just look at the solution._"]
    return tgtext.to_html("\n".join(body)), _kb(
        [("Show solution", f"study:{sid}:{idx}:o")], tail)


def _send(api: Api, row: sqlite3.Row, html: str, markup: dict | None = None) -> None:
    extra = {"message_thread_id": row["thread_id"]} if row["thread_id"] else {}
    if markup:
        extra["reply_markup"] = markup
    resp = api("sendMessage", chat_id=row["chat_id"], text=html, parse_mode="HTML",
               link_preview_options={"is_disabled": True}, **extra)
    if not (resp and resp.get("ok")):
        # A question that fails to render must still arrive, or the quiz
        # stalls with nothing on screen to answer.
        plain = _html.unescape(re.sub(r"<[^>]+>", "", html))
        api("sendMessage", chat_id=row["chat_id"], text=plain,
            link_preview_options={"is_disabled": True}, **extra)


def send_current(api: Api, con: sqlite3.Connection, set_id: int) -> None:
    row = con.execute("SELECT * FROM study_sets WHERE id = ?", (set_id,)).fetchone()
    html, kb = question(row, row["pos"])
    _send(api, row, html, kb)


def start(api: Api, con: sqlite3.Connection, set_id: int) -> None:
    row = con.execute("SELECT * FROM study_sets WHERE id = ?", (set_id,)).fetchone()
    spec = json.loads(row["spec"])
    intro = spec.get("intro")
    n = len(spec["items"])
    lead = f"**{row['title']}**, {n} question{'s' if n != 1 else ''}."
    _send(api, row, tgtext.to_html(f"{lead}\n{intro}" if intro else lead))
    send_current(api, con, set_id)


# -- answering ----------------------------------------------------------------

def _record(con, set_id, idx, *, answer=None, correct=None, rating=None):
    con.execute(
        "INSERT INTO study_answers (set_id, idx, ts, answer, correct, rating) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (set_id, idx) DO UPDATE SET "
        "answer = COALESCE(excluded.answer, answer), "
        "correct = COALESCE(excluded.correct, correct), "
        "rating = COALESCE(excluded.rating, rating), ts = excluded.ts",
        (set_id, idx, db.now(), answer, correct, rating))


def _claim(con, set_id: int, idx: int, awaiting: str, then: str | None,
           pos: int | None = None) -> bool:
    """Move the set on only if it is still where this tap thinks it is.
    Two quick taps on the same button arrive on two threads; exactly one of
    them may advance, or a question gets skipped."""
    cur = con.execute(
        "UPDATE study_sets SET awaiting = ?, pos = ? WHERE id = ? AND pos = ? "
        "AND awaiting = ? AND state = 'active'",
        (then, idx if pos is None else pos, set_id, idx, awaiting))
    con.commit()
    return cur.rowcount == 1


def _solution_block(item: dict) -> str:
    return f"\n\n**Solution**\n{item['solution']}" if item.get("solution") else ""


def _advance(api: Api, con, set_id: int, idx: int) -> dict | None:
    """Past the last item this closes the set and returns the review the
    caller should hand to a session; otherwise it sends the next question."""
    row = con.execute("SELECT * FROM study_sets WHERE id = ?", (set_id,)).fetchone()
    total = len(json.loads(row["spec"])["items"])
    if idx + 1 < total:
        con.execute("UPDATE study_sets SET pos = ?, awaiting = 'answer' WHERE id = ?",
                    (idx + 1, set_id))
        con.commit()
        send_current(api, con, set_id)
        return None
    return finish(api, con, set_id)


def on_callback(api: Api, con: sqlite3.Connection, data: str) -> tuple[str, dict | None]:
    """Returns (toast text, a review to hand to a session or None)."""
    parts = data.split(":")
    if len(parts) < 3 or not parts[1].isdigit():
        return "", None
    set_id = int(parts[1])
    row = con.execute("SELECT * FROM study_sets WHERE id = ?", (set_id,)).fetchone()
    if not row or row["state"] != "active":
        return "That quiz is over", None
    if parts[2] == "x":
        return "Ended", finish(api, con, set_id, ended_early=True)

    idx, action = int(parts[2]), parts[3] if len(parts) > 3 else ""
    items = json.loads(row["spec"])["items"]
    item = items[idx]

    if action == "s":
        if not _claim(con, set_id, idx, row["awaiting"], None):
            return "", None
        _record(con, set_id, idx, rating="skipped")
        return "Skipped", _advance(api, con, set_id, idx)

    if action == "c" and item["kind"] == "choice":
        if not _claim(con, set_id, idx, "answer", None):
            return "Already answered", None
        pick = int(parts[4])
        right = pick == item["answer"]
        _record(con, set_id, idx, answer=LETTERS[pick], correct=int(right))
        fb = (item.get("feedback") or [""] * len(item["options"]))[pick]
        verdict = "Right." if right else f"Not quite, it's {LETTERS[item['answer']]}."
        _send(api, row, tgtext.to_html(
            f"{'✅' if right else '❌'} {verdict}" + (f" {fb}" if fb else "")
            + ("" if right else _solution_block(item))))
        return ("Right" if right else "Not quite"), _advance(api, con, set_id, idx)

    if action == "o" and item["kind"] == "open":
        if not _claim(con, set_id, idx, "answer", "rating"):
            return "", None
        _reveal(api, row, idx, item)
        return "", None

    if action == "r" and item["kind"] == "open":
        if not _claim(con, set_id, idx, "rating", None):
            return "Already rated", None
        _record(con, set_id, idx, rating=RATINGS.get(parts[4], parts[4]))
        return RATINGS.get(parts[4], ""), _advance(api, con, set_id, idx)

    if action == "n":
        # "That wasn't an answer." A typed message is the answer by default,
        # so a conversational one gets eaten -- on 14 Sep 2026 "finished my
        # exam, end this quiz and quiz me on something else" was recorded as
        # the attempt at a simplification. One tap gives it back to the
        # session as the message it was, and an open item waits for a real
        # attempt again.
        ans = con.execute("SELECT answer FROM study_answers WHERE set_id = ? AND idx = ?",
                          (set_id, idx)).fetchone()
        if not ans or ans["answer"] is None:
            return "", None
        con.execute("DELETE FROM study_answers WHERE set_id = ? AND idx = ?", (set_id, idx))
        if item["kind"] == "open":
            con.execute("UPDATE study_sets SET awaiting = 'answer' WHERE id = ? AND pos = ? "
                        "AND awaiting = 'rating'", (set_id, idx))
        con.commit()
        return "Passing it to Herald", {
            "kind": "relay", "set_id": set_id, "chat_id": row["chat_id"],
            "thread_id": row["thread_id"],
            "prompt": ("(Sent while a quiz was running in this topic. The quiz took it "
                       "as an answer and they tapped \"that wasn't an answer\"; "
                       "`herald quiz stop` ends the quiz if they want that.)\n\n"
                       + ans["answer"])}

    if action == "k" and item["kind"] == "open":
        # Check this one now: the caller runs a session on it. The quiz does
        # not wait -- the rating buttons stay live.
        ans = con.execute("SELECT answer FROM study_answers WHERE set_id = ? AND idx = ?",
                          (set_id, idx)).fetchone()
        return "Asking Herald", {"kind": "check", "set_id": set_id, "chat_id": row["chat_id"],
                                 "thread_id": row["thread_id"],
                                 "prompt": check_prompt(item, ans["answer"] if ans else "")}
    return "", None


def _reveal(api: Api, row, idx: int, item: dict, attempted: bool = False) -> None:
    sid = row["id"]
    rows = [[("Got it", f"study:{sid}:{idx}:r:g"), ("Partial", f"study:{sid}:{idx}:r:p"),
             ("Missed", f"study:{sid}:{idx}:r:m")]]
    if attempted:
        rows.append([("Check my answer now", f"study:{sid}:{idx}:k")])
        rows.append([("That wasn't an answer", f"study:{sid}:{idx}:n")])
    text = f"**Solution**\n{item['solution']}\n\n_How did you do?_"
    _send(api, row, tgtext.to_html(text), _kb(*rows))


def on_text(api: Api, con: sqlite3.Connection, chat_id: int, thread_id: int | None,
            text: str) -> tuple[bool, dict | None]:
    """A typed message in a topic with a live set. Returns (handled, review).

    Only an answer the set is waiting for is swallowed, and anything taken
    as an answer that was not one comes back with one tap (action `n`). A message while the
    set waits on a rating button is conversation, and goes to the session as
    usual -- as does anything starting with `/` except `/skip` and `/endquiz`.
    """
    row = active_for(con, chat_id, thread_id)
    if not row:
        return False, None
    cmd = text.strip().lower().split("@")[0]
    if cmd == "/endquiz":
        return True, finish(api, con, row["id"], ended_early=True)
    if cmd == "/skip":
        toast, review = on_callback(api, con, f"study:{row['id']}:{row['pos']}:s")
        return True, review
    if text.startswith("/") or row["awaiting"] != "answer":
        return False, None

    idx = row["pos"]
    item = json.loads(row["spec"])["items"][idx]
    if item["kind"] == "choice":
        letter = text.strip().upper()[:1]
        if len(text.strip()) == 1 and letter in LETTERS[:len(item["options"])]:
            _, review = on_callback(api, con,
                                    f"study:{row['id']}:{idx}:c:{LETTERS.index(letter)}")
            return True, review
        return False, None

    if item["kind"] == "short":
        if not _claim(con, row["id"], idx, "answer", None):
            return True, None
        right = is_correct(item, text)
        _record(con, row["id"], idx, answer=text, correct=int(right))
        fb = item.get("feedback_right" if right else "feedback_wrong") or ""
        shown = "" if right else f" Expected: {item.get('display') or item['accept'][0]}"
        _send(api, row, tgtext.to_html(
            f"{'✅ Right.' if right else '❌ Not quite.'}{shown}"
            + (f"\n{fb}" if fb else "") + _solution_block(item)),
            None if right else _kb([("That wasn't an answer", f"study:{row['id']}:{idx}:n")]))
        return True, _advance(api, con, row["id"], idx)

    if not _claim(con, row["id"], idx, "answer", "rating"):
        return True, None
    _record(con, row["id"], idx, answer=text)
    _reveal(api, row, idx, item, attempted=True)
    return True, None


# -- finishing ----------------------------------------------------------------

def finish(api: Api, con: sqlite3.Connection, set_id: int,
           ended_early: bool = False) -> dict | None:
    cur = con.execute("UPDATE study_sets SET state = 'done', finished = ?, awaiting = NULL "
                      "WHERE id = ? AND state = 'active'", (db.now(), set_id))
    con.commit()
    if cur.rowcount != 1:
        return None
    row = con.execute("SELECT * FROM study_sets WHERE id = ?", (set_id,)).fetchone()
    items = json.loads(row["spec"])["items"]
    answers = {a["idx"]: a for a in con.execute(
        "SELECT * FROM study_answers WHERE set_id = ?", (set_id,))}

    checked = [i for i, it in enumerate(items) if it["kind"] != "open" and i in answers
               and answers[i]["correct"] is not None]
    right = sum(1 for i in checked if answers[i]["correct"])
    rated = [answers[i]["rating"] for i, it in enumerate(items)
             if it["kind"] == "open" and i in answers and answers[i]["rating"]]
    line = []
    if checked:
        line.append(f"{right}/{len(checked)} checked answers right")
    if rated:
        line.append("self-rated: " + ", ".join(f"{rated.count(r)} {r}"
                                                for r in dict.fromkeys(rated)))
    unanswered = len(items) - len(answers)
    if unanswered:
        line.append(f"{unanswered} not reached")
    summary = ("Ended early. " if ended_early else "Done. ") + ("; ".join(line) or "nothing answered") + "."
    con.execute("UPDATE study_sets SET summary = ? WHERE id = ?", (summary, set_id))
    con.commit()
    row = con.execute("SELECT * FROM study_sets WHERE id = ?", (set_id,)).fetchone()
    _send(api, row, tgtext.to_html(
        summary + ("\n\n_Herald is reading your written answers now._" if answers else "")))
    if not answers:
        return None
    return {"kind": "review", "set_id": set_id, "chat_id": row["chat_id"],
            "thread_id": row["thread_id"], "prompt": review_prompt(row, items, answers)}


def review_prompt(row, items: list[dict], answers: dict) -> str:
    out = [f"[The quiz engine wrote this message, not the user. Set #{row['id']} "
           f"\"{row['title']}\"" + (f", area {row['area']}" if row["area"] else "")
           + f" just ended: {row['summary']}]", "",
           "Assess it following the herald-study skill: grade each written answer "
           "against its solution (the self-rating is their own first pass, correct it "
           "where it was generous or harsh), record what they know in the ledger, and "
           "reply with what they got wrong and what to review, briefly. Keep it to "
           "what matters for their next attempt.", ""]
    for i, it in enumerate(items):
        a = answers.get(i)
        out.append(f"## Q{i + 1} ({it['kind']}{', ' + it['topic'] if it.get('topic') else ''})")
        out.append(it["prompt"])
        if it["kind"] == "choice":
            out.append("Options: " + " | ".join(f"{LETTERS[j]}. {o}" for j, o in enumerate(it["options"]))
                       + f" -- correct {LETTERS[it['answer']]}")
        elif it["kind"] == "short":
            out.append(f"Accepted: {it['accept']}")
        if it.get("solution"):
            out.append(f"Solution: {it['solution']}")
        if not a:
            out.append("Their answer: (not reached)")
        else:
            out.append(f"Their answer: {a['answer'] if a['answer'] is not None else '(none typed)'}")
            if a["correct"] is not None:
                out.append(f"Auto-check: {'right' if a['correct'] else 'wrong'}")
            if a["rating"]:
                out.append(f"Self-rating: {a['rating']}")
        out.append("")
    return "\n".join(out)


def check_prompt(item: dict, answer: str) -> str:
    return ("[The quiz engine wrote this message, not the user. They asked for "
            "one written answer to be checked now, mid-quiz.]\n\n"
            f"Question: {item['prompt']}\n\nSolution: {item['solution']}\n\n"
            f"Their answer: {answer or '(none typed)'}\n\n"
            "Say in a few sentences whether it would earn full credit and exactly "
            "what is wrong or missing. The quiz is still running; do not start "
            "anything else.")
