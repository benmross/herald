"""The stock patterns of machine-written prose, found deterministically.

A rule in the constitution asks every session to write plainly. That is not
enough on its own, and the people who have tried say so consistently: an
instruction about style holds for a few turns, fades as the context grows, and
is gone after a compaction. What held for them was a check that does not depend
on the model remembering anything. This is that check. `tools/prose_hook.py`
runs it on a reply as the session is about to end its turn, and `herald prose`
runs it on a file written for somebody else.

**The patterns are symptoms.** Wikipedia's field guide (WP:AISIGNS) says it of
its own list: "Please do not merely treat these signs as the problems to be
fixed." An em dash swapped for a comma is a comma splice, and a contrast with
its "not just" deleted is still a sentence that announces a point instead of
making one. So every rule here carries advice about the sentence, and the
hook asks for the sentence to be rewritten, never patched.

**Two weights.** A hard rule is a phrase almost nobody writes by accident
("here's the kicker", "you're right to push back"). A soft rule is a shape that
honest prose also takes now and then: one soft hit passes, two do not. That
keeps "the exam isn't Wednesday, it's Thursday" from costing a rewrite.

**Quoted text is never linted.** Code, blockquotes, URLs and anything inside
quotation marks are blanked before matching, so a session can quote an email
that is full of these, or name a pattern in order to discuss it.

Where the rules came from: the Hacker News threads on "Various LLM Smells"
(item 48313810) and "Make Claude stop talking like a BuzzFeed article"
(49388752), and Wikipedia:Signs of AI writing. The list will date. Each rule
can be switched off under `prose.off` in the config without touching this file.

**Pure and standard-library only**, like safety.py and for the same reason: the
hook loads it by path at the end of every turn in every session.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HARD, SOFT = "hard", "soft"
#: this many soft hits, with no hard one, is still a flag
SOFT_LIMIT = 2


@dataclass(frozen=True)
class Rule:
    name: str
    weight: str
    pattern: re.Pattern
    advice: str


@dataclass(frozen=True)
class Hit:
    rule: str
    weight: str
    excerpt: str
    line: int
    advice: str


def _rx(pattern: str, flags: int = re.I) -> re.Pattern:
    return re.compile(pattern, flags)


def _alt(*phrases: str) -> str:
    return r"\b(?:" + "|".join(phrases) + r")\b"


RULES: list[Rule] = [
    Rule("em_dash", HARD, _rx(r"—|\s–\s|(?<=\w) -- (?=\w)"),
         "no em dash. Restructure with a colon, a full stop or parentheses; a "
         "comma dropped in its place is usually a splice"),
    Rule("not_just", HARD, _rx(
        r"\b(?:is|are|was|were|it's|that's)(?: not|n't) "
        r"(?:just|merely|simply|only) (?:a |an |the |about )"
        r"|\b(?:doesn't|don't|didn't) (?:just|merely) \w+[^.?!\n]{0,80}[;,] (?:it|they|but)\b"
        r"|\bnot (?:just|merely) [^.?!\n]{1,80}(?:, but\b|; it\b|, it's\b)"
        r"|\bnot only\b[^.?!\n]{1,120}\bbut\b"
        r"|\bless about\b[^.?!\n]{1,60}\bmore about\b"),
         "say what it is. Introducing a point by denying a weaker one nobody "
         "raised is the most recognised pattern there is"),
    Rule("no_no_just", HARD, _rx(
        r"\bno [\w-]+(?: [\w-]+)?[,.] no [\w-]+(?: [\w-]+)?[,.] (?:just|only)\b"),
         "state the fact once, as a sentence"),
    Rule("signpost", HARD, _rx(_alt(
        r"here(?:'s| is) the (?:thing|kicker|catch|twist|problem|deal|rub)",
        r"here(?:'s| is) where it gets", r"here's what(?:'s| is) (?:interesting|wild)",
        r"(?:and )?(?:this|that) is the kicker", r"the kicker",
        r"the (?:honest|real|short|uncomfortable|hard) (?:answer|truth)",
        r"(?:honest|genuine) caveat", r"the thing to internalize",
        r"the real (?:question|issue|story|lesson|move|footnote)",
        r"that's the real", r"let's (?:dive|dig|unpack|break (?:it|this) down)",
        r"let me be (?:clear|honest|direct)", r"to be clear",
        r"it(?:'s| is) worth (?:noting|mentioning|pointing out|flagging)",
        r"worth noting", r"it bears (?:mentioning|repeating)",
        r"at the end of the day", r"when all is said and done",
        r"the (?:key )?takeaway", r"key insight", r"what jumped out",
        r"the most (?:instructive|interesting|important) (?:one|part|yet)")),
         "cut the announcement and write the thing it was announcing"),
    Rule("flattery", HARD, _rx(
        r"\A\W*(?:you're|you are) (?:absolutely |completely |totally |quite )?(?:right|correct)\b"
        r"|\b(?:absolutely|totally|completely) right\b"
        r"|\bright to push back\b"
        r"|\b(?:great|good|excellent|fantastic|fair|sharp) (?:question|catch|point|call|instinct|insight|observation|take)\b"
        r"|\A\W*(?:certainly|of course|absolutely|sure thing)[!,.]"),
         "skip the verdict on what they said and answer it. If they were right, "
         "the correction itself shows that"),
    Rule("closer", HARD, _rx(_alt(
        r"(?:i )?hope (?:this|that) helps", r"let me know if",
        r"feel free to", r"happy to (?:help|dig|elaborate|walk|go deeper|expand)",
        r"is there anything else", r"don't hesitate to")),
         "end on the last thing that carries information"),
    Rule("recap", HARD, _rx(
        r"^[ \t>*_]*(?:in summary|to summari[sz]e|in conclusion|to sum up|in short"
        r"|overall|bottom line|tl;?dr|the bottom line)\b[,:]", re.I | re.M),
         "do not restate what the reply just said. If it needs a summary it is "
         "too long"),
    Rule("stock_phrase", HARD, _rx(_alt(
        r"load[- ]bearing", r"blast radius", r"delv(?:e|es|ed|ing)", r"tapestry",
        r"(?:a|is a|as a) testament to", r"game[- ]changer", r"deep dive",
        r"smoking gun", r"north star", r"at its core", r"ever[- ]evolving",
        r"stands as a", r"serves as a (?:reminder|testament)",
        r"plays? a (?:crucial|pivotal|key|vital|significant) role",
        r"(?:rich|vibrant) (?:history|heritage|culture|tapestry)",
        r"evolving landscape", r"in today's (?:fast-paced |digital )?world",
        r"double[- ]edged sword", r"paints? a picture", r"sheds? light on")),
         "use the plain word for what you mean"),
    Rule("not_x_its_y", SOFT, _rx(
        r"\b(?:is|are|was|were|it's|that's)(?:n't| not) (?:[\w'-]+ ){0,5}[\w'-]+"
        r"[.,;:] (?:it|this|that|they)(?:'s|'re| is| are| was) "
        r"|\b(?:is|are|was|were)(?:n't| not) (?:[\w'-]+ ){0,4}[\w'-]+\. "
        r"(?:[\w'-]+ ){1,5}(?:is|are|was|were|does|did)\."
        r"|\b(you're|we're|they're|i'm) not [^.?!\n]{1,50}, \1 "),
         "say what it is. Keep the denial only if somebody actually believed it"),
    Rule("intensifier", SOFT, _rx(_alt(
        r"genuinely", r"honestly", r"truly", r"frankly", r"to be honest",
        r"quietly", r"crucial(?:ly)?", r"pivotal", r"seamless(?:ly)?",
        r"surgical(?:ly)?", r"meticulous(?:ly)?", r"underscor(?:e|es|ed|ing)",
        r"showcas(?:e|es|ed|ing)", r"foster(?:s|ed|ing)?", r"leverag(?:e|es|ed|ing)",
        r"robust", r"nuanced", r"the shape of")),
         "a word that vouches for the sentence instead of adding to it. Delete "
         "it and see whether anything was lost"),
    Rule("label_colon", SOFT, _rx(
        r"(?:^|(?<=[.!?] ))[ \t]*(?:The|One|My) (?:[a-z]+ ){0,2}(?:catch|fix|result|upshot"
        r"|point|problem|issue|trick|key|answer|lesson|twist|reason|caveat|irony"
        r"|gap|risk|trade-?off|difference|verdict|punchline|good news|bad news)"
        r"(?: here)?:", re.M),
         "write the sentence without the label"),
    Rule("question_answer", SOFT, _rx(
        r"(?:^|(?<=[.!?] ))[ \t]*(?!(?:what|why|how|when|where|who|which|want|should"
        r"|shall|do|does|did|is|are|was|were|can|could|would|will|have|has|any"
        r"|ok|okay|ready|really|right|sure|yes|no)\b)"
        r"(?:[A-Za-z'-]+ ){0,3}[A-Za-z'-]+\? (?=[A-Z])", re.I | re.M),
         "a question asked only so it can be answered. State the answer"),
]

_BY_NAME = {r.name: r for r in RULES}
#: rules that are counted over the whole text instead of matched
COUNTED = {
    "staccato": (SOFT, "very short sentences in a row, written for rhythm. "
                       "Join them into one that says the thing"),
    "bold_list": (SOFT, "a list whose every item opens with a bold label. "
                        "Write it as sentences, or as a plain list"),
    "bold": (SOFT, "bold scattered through running text. Keep it for the one "
                   "thing the reader must not miss, or drop it"),
}
RULE_NAMES = tuple(_BY_NAME) + tuple(COUNTED)


def _blank(match: re.Match) -> str:
    """Same length, newlines kept, so offsets and line numbers still hold."""
    return re.sub(r"[^\n]", " ", match.group(0))


_QUOTED = [
    re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.S | re.M),     # fenced code
    re.compile(r"`[^`\n]+`"),                                 # inline code
    re.compile(r"^[ \t]*>[^\n]*$", re.M),                     # blockquote
    re.compile(r"^(?: {4}|\t)[^\n]*$", re.M),                 # indented code
    re.compile(r"https?://\S+"),
    re.compile(r"\"[^\"\n]{1,400}\""),
    re.compile(r"“[^”\n]{1,400}”"),
    re.compile(r"«[^»\n]{1,400}»"),
]


def visible(text: str) -> str:
    """`text` with everything quoted or literal blanked out."""
    for rx in _QUOTED:
        text = rx.sub(_blank, text)
    return text


def _excerpt(text: str, start: int, end: int, pad: int = 28) -> str:
    a, b = max(0, start - pad), min(len(text), end + pad)
    cut = " ".join(text[a:b].split())
    return ("…" if a else "") + cut + ("…" if b < len(text) else "")


def _is_staccato(run: list) -> bool:
    """Three short sentences running, or two that are a word or two each."""
    return len(run) >= 3 or (len(run) == 2 and all(n <= 2 for _, n in run))


def _staccato(text: str) -> list[tuple[int, int]]:
    out = []
    for para in re.finditer(r"[^\n]+", text):
        line = para.group(0)
        if re.match(r"\s*(?:[-*•]|\d+[.)])\s", line):
            continue
        run = []
        for s in re.finditer(r"[^.!?]+[.!?]+(?=\s|$)", line):
            words = re.findall(r"[A-Za-z'-]+", s.group(0))
            if 1 <= len(words) <= 3 and not re.search(r"\d", s.group(0)):
                run.append((s, len(words)))
                continue
            if _is_staccato(run):
                out.append((para.start() + run[0][0].start(),
                            para.start() + run[-1][0].end()))
            run = []
        if _is_staccato(run):
            out.append((para.start() + run[0][0].start(),
                        para.start() + run[-1][0].end()))
    return out


def lint(text: str, off: tuple | list | set = ()) -> list[Hit]:
    """Every hit in `text`, in the order it reads."""
    off = set(off or ())
    seen = visible(text)
    found: list[tuple[int, Hit]] = []

    def add(name: str, weight: str, advice: str, start: int, end: int) -> None:
        found.append((start, Hit(name, weight, _excerpt(text, start, end),
                                 text.count("\n", 0, start) + 1, advice)))

    for rule in RULES:
        if rule.name in off:
            continue
        for m in rule.pattern.finditer(seen):
            add(rule.name, rule.weight, rule.advice, m.start(), m.end())

    if "staccato" not in off:
        for start, end in _staccato(seen):
            add("staccato", *COUNTED["staccato"], start, end)
    if "bold_list" not in off:
        items = list(re.finditer(
            r"^[ \t]*(?:[-*•]|\d+[.)])[ \t]+\*\*[^*\n]{1,50}\*\*", seen, re.M))
        if len(items) >= 3:
            add("bold_list", *COUNTED["bold_list"], items[0].start(), items[0].end())
    if "bold" not in off:
        spans = [m for m in re.finditer(r"\*\*[^*\n]{1,60}\*\*", seen)
                 if not re.match(r"[ \t]*(?:[-*•]|\d+[.)])[ \t]+$",
                                 seen[seen.rfind("\n", 0, m.start()) + 1: m.start()])]
        if len(spans) >= 4 and len(spans) * 250 > len(seen):
            add("bold", *COUNTED["bold"], spans[0].start(), spans[0].end())

    found.sort(key=lambda pair: pair[0])
    return [hit for _, hit in found]


def flagged(hits: list[Hit]) -> bool:
    """One hard hit, or enough soft ones that it is no longer an accident."""
    if any(h.weight == HARD for h in hits):
        return True
    return sum(1 for h in hits if h.weight == SOFT) >= SOFT_LIMIT


def report(hits: list[Hit], limit: int = 12) -> str:
    """One line per hit, with the advice given once per rule."""
    lines, advised = [], set()
    for h in hits[:limit]:
        line = f"  {h.rule} (line {h.line}): {h.excerpt}"
        if h.rule not in advised:
            advised.add(h.rule)
            line += f"\n      {h.advice}"
        lines.append(line)
    if len(hits) > limit:
        lines.append(f"  and {len(hits) - limit} more")
    return "\n".join(lines)
