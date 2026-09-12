"""What each outward action is: green, amber or red.

The constitution's three tiers used to live in three places that each encoded
them separately and disagreed at the edges: prose in the constitution, a regex
in tools/check.py that knew about Calendar and Gmail but not Drive, and a
`tier="amber"` default on every gwrite function that nothing ever validated.
An audit on 12 Sep 2026 found the gap the disagreement left: a conversation
uploaded a file to Drive through a runtime script, which is amber by the
constitution's own words, and it never touched gwrite, never hit the regex, and
was logged only because the session happened to remember to.

So this module is the one place the classification lives, as data, and every
enforcement point reads it:

    tools/check.py        which tracked files may call which Google methods
    tools/guard.py        the Claude Code hook that stops a session's runtime
                          script from doing the same thing outside the door
    lib/herald/gwrite.py  validates the tier it records
    lib/herald/red.py     the only module that carries out a red action

The tiers, restated as what they mean for code:

    green   read-only, or a draft nobody sees. No gate, no log required.
    amber   a reversible write only the user sees. Must go through gwrite,
            which logs it before returning, so the digest can report it.
    red     another person sees it, it is irreversible, or it spends money.
            Must go through red.py, which refuses to act without an approval
            the user tapped on their phone.

**A classifier for a safety control has to be conservative in one direction
only.** Anything this does not recognise as a read is treated as a write, and
anything ambiguous between amber and red is red. `reply` and `forward` are red
even though a connector might in principle stage them, because the cost of
being wrong the other way is a message to a real person.

Detection is AST-based on purpose. The first detector was a regex over text,
and the very audit that motivated this matched its own journal entry -- a
heredoc of prose *describing* `permissions().create()` -- as a real sharing
call. A string literal or a comment that names a method is a Constant node, not
a Call, so parsing tells code from prose where a regex cannot. The regex remains
only as a fallback for fragments that do not parse, with strings and comments
stripped first.

What this is not: a sandbox. A session determined to evade it can build a
method name at runtime. It is a tripwire against the accidental bypass that
actually happened, and the thing that stops a *fooled* session is the tap in
red.py, which no amount of code in the session can perform.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass

GREEN, AMBER, RED = "green", "amber", "red"
TIERS = (GREEN, AMBER, RED)

# --------------------------------------------------------------------------
# Google API client calls: `<resource>().<method>(`
# --------------------------------------------------------------------------

# Methods that never change anything. Everything else on a known resource is a
# write, which is the conservative default.
READ_METHODS = frozenset({
    "get", "list", "list_next", "export", "export_media", "get_media", "watch",
    "query", "search", "batchGet", "getByDataFilter", "batchGetByDataFilter",
    "searchContacts", "listDirectoryPeople", "searchDirectoryPeople",
    "getBatchGet", "instances", "quickAdd_preview", "freebusy",
})

# (resource, method) pairs that are red regardless of which service they are on.
# Sending reaches another person. Permissions share with another person.
# `delete` on mail and files bypasses the trash, and the constitution says trash,
# never delete. `emptyTrash` makes every prior trash irreversible at once.
_RED_PAIRS = {
    ("messages", "send"), ("drafts", "send"), ("messages", "import_"),
    ("messages", "insert"),
    ("messages", "delete"), ("threads", "delete"), ("messages", "batchDelete"),
    ("permissions", "create"), ("permissions", "update"), ("permissions", "patch"),
    ("permissions", "delete"),
    ("files", "delete"), ("files", "emptyTrash"),
    ("drives", "create"), ("drives", "delete"),
}

# Drafts are green: the constitution says so, because nobody sees a draft until
# the user sends it themselves.
_GREEN_PAIRS = {("drafts", "create"), ("drafts", "update")}

# Resources whose writes are amber unless listed above. A resource not here is
# still classified -- as amber if the method is a write -- so a new Google API
# cannot slip through for want of being named.
GOOGLE_RESOURCES = frozenset({
    "events", "calendars", "calendarList", "acl", "settings",
    "tasks", "tasklists",
    "labels", "messages", "drafts", "threads", "history", "filters",
    "forwardingAddresses", "sendAs", "delegates",
    "files", "permissions", "revisions", "comments", "replies", "drives",
    "spreadsheets", "values", "sheets", "documents",
    "people", "contactGroups", "otherContacts",
})

# Resources where a *write* reaches other people by construction, so it is red
# even though the method name looks harmless: an ACL entry on a calendar shares
# it, a filter or forwarding address silently redirects future mail, and a
# delegate can read and send as the user.
_RED_RESOURCES = frozenset({"acl", "filters", "forwardingAddresses", "sendAs",
                            "delegates"})


def classify_google(resource: str, method: str) -> str:
    """Tier for one `resource().method()` call."""
    if method in READ_METHODS:
        return GREEN
    if (resource, method) in _RED_PAIRS or resource in _RED_RESOURCES:
        return RED
    if (resource, method) in _GREEN_PAIRS:
        return GREEN
    return AMBER


# --------------------------------------------------------------------------
# Connector tools a session can call directly: mcp__<server>__<tool>
# --------------------------------------------------------------------------

_MCP_RED = re.compile(
    r"(^|_)(send|forward|reply|share|invite|publish|post|delete_(message|thread|file)"
    r"|permanently|empty_trash)(_|$)")
_MCP_GREEN = re.compile(
    r"(^|_)(get|list|search|read|fetch|find|query|view|download|export)(_|$)"
    r"|(^|_)(create|update)_draft(_|$)|(^|_)get_draft")


def classify_mcp(tool_name: str) -> str | None:
    """Tier for a connector tool, or None if it is not a connector tool.

    Classified by the verb in the tool's name. That is heuristic by nature --
    connectors name things however they like -- so the unknown case falls to
    amber, never green, and anything that sounds like reaching a person is red.
    """
    if not tool_name.startswith("mcp__"):
        return None
    verb = tool_name.split("__")[-1].lower()
    if _MCP_RED.search(verb):
        return RED
    if _MCP_GREEN.search(verb):
        return GREEN
    return AMBER


# --------------------------------------------------------------------------
# Finding calls in code
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Call:
    resource: str
    method: str
    tier: str
    line: int

    def __str__(self) -> str:
        return f"{self.resource}().{self.method}() [{self.tier}] line {self.line}"


# Names that google-api-python-client uses as resource accessors rather than as
# API methods. A zero-argument call to one of these navigates to a sub-resource,
# as in svc.users().messages() or svc.people().connections().
ACCESSOR_NAMES = GOOGLE_RESOURCES | frozenset({
    "users", "connections", "members", "developerMetadata", "calendarList",
    "colors", "about", "changes", "channels", "teamdrives", "apps", "history",
    "settings",
})


def _is_execute(name: str) -> bool:
    return name == "execute" or name.startswith("execute_")


def _chain(call: ast.Call) -> list[str]:
    """The accessor names leading up to a call, outermost first.

    For svc.users().settings().filters().create(...) that is
    [users, settings, filters].
    """
    names = []
    v = call.func.value
    while isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute):
        names.append(v.func.attr)
        v = v.func.value
    names.reverse()
    return names


def _from_ast(tree: ast.AST) -> list[Call]:
    """Every Google API method call in parsed code, classified.

    The client library nests resources, so a call chain is navigation followed
    by one method: svc.people().connections().list(...) navigates to people,
    then connections, and calls list. The first version of this read each
    adjacent pair as (resource, method), so the accessor in the middle came out
    as a method, and people().connections() was classified as an amber write.
    A read-only contacts collector failed `herald check` on a clean checkout
    because of it (found by the bare-checkout test, 12 Sep 2026).

    So a call is only a method if it is not navigation. It is navigation when
    its result is immediately called into by something other than execute(),
    or when it is a zero-argument call to a known accessor name, which also
    covers an accessor saved to a variable and used on a later line. Zero
    arguments alone does not make a call an accessor: files().emptyTrash() takes
    none and is red.
    """
    navigated = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Call)
                and not _is_execute(node.func.attr)):
            navigated.add(id(node.func.value))

    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        method = node.func.attr
        if _is_execute(method):
            continue
        chain = _chain(node)
        if not chain or not any(n in GOOGLE_RESOURCES for n in chain):
            continue
        zero_arg = not node.args and not node.keywords
        if id(node) in navigated or (zero_arg and method in ACCESSOR_NAMES):
            continue
        resource = chain[-1]
        found.append(Call(resource, method, classify_google(resource, method),
                          getattr(node, "lineno", 0)))
    return found

# Whitespace-tolerant, because a fragment that tokenizes is rejoined with spaces
# between tokens; the first version required .name() with none and so matched
# nothing at all on that path, silently missing a red call. And the method sits
# in a lookahead so nested pairs overlap: without it, matching people().connections(
# consumed connections( and the following connections().list( could never match.
_CALL_RE = re.compile(r"\.\s*(\w+)\s*\(\s*\)\s*(?=\.\s*(\w+)\s*\()")


def _strip_strings_and_comments(src: str) -> str:
    """Blank out string literals and comments, keeping line structure.

    For fragments that do not parse as a whole. Tokenising still works on most
    of them, and it is what lets the fallback ignore prose the way the AST path
    does.
    """
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                out.append(" ")
            else:
                out.append(tok.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # Unterminated input. Drop anything inside quotes crudely rather than
        # scanning prose as code.
        return re.sub(r"(\"\"\"|''')[\s\S]*?(\1|$)|\"[^\"\n]*\"|'[^'\n]*'|#.*",
                      " ", src)
    return " ".join(out)


def find_calls(src: str) -> list[Call]:
    """Google API calls in a piece of Python source, classified."""
    try:
        return _from_ast(ast.parse(src))
    except (SyntaxError, ValueError):
        pass
    code = _strip_strings_and_comments(src)
    found = []
    for m in _CALL_RE.finditer(code):
        resource, method = m.group(1), m.group(2)
        # A regex cannot see arguments or what follows, so it leans on names:
        # skip a pair whose "method" is really an accessor, and accept any
        # accessor as the resource so nested writes are still caught.
        if _is_execute(method) or method in ACCESSOR_NAMES:
            continue
        if resource in ACCESSOR_NAMES:
            found.append(Call(resource, method, classify_google(resource, method), 0))
    return found


def writes(src: str) -> list[Call]:
    """Only the calls that change something."""
    return [c for c in find_calls(src) if c.tier != GREEN]


def worst(calls) -> str:
    """The highest tier among a set of calls, or green for none."""
    tiers = {c.tier if isinstance(c, Call) else c for c in calls}
    if RED in tiers:
        return RED
    if AMBER in tiers:
        return AMBER
    return GREEN


def validate_tier(tier: str) -> str:
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}; must be one of {TIERS}")
    return tier
