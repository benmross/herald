"""What an agent reads and does on a page: element refs and batched steps.

Why this exists: on 13 Nov 2026 a LinkedIn profile edit took about seventy
tool calls. Every `herald browse` command was its own process, so it
reconnected, re-tagged fields (handles shifted between calls) and slept a
guessed time. Clicking "by text" hit a paragraph instead of the button next
to it. Radios, checkboxes and rich-text boxes were missing from `fields`. A
form defaulting to "replace your headline" was nearly missed for that reason.

What other agent browsers do, surveyed that night and again on 21 Nov:
Vercel's agent-browser and Microsoft's Playwright CLI both keep a browser
connection alive in a daemon, print the page as an accessibility tree with a
short ref per element (`e7`), act on refs, and let several steps run in one
call. Both read refs from Playwright's own AI snapshot
(`aria_snapshot(mode="ai")` and the `aria-ref=` selector), which Herald's
Playwright already ships. Herald uses that engine directly rather than either
CLI because they would sit beside Herald's guards instead of under them:
the submit refusal, protected fields, tab ownership, application checkpoints
and the page-content label.

Refs live in the Playwright connection that took the snapshot, which is one
reason a single daemon (browse_daemon.py) holds that connection. This module
is the part of it that knows about pages: the snapshot an agent reads, the
steps it can run, and the approval that lets one refused click through.
Everything here that touches a page is async and runs inside the daemon.
"""
from __future__ import annotations

import contextlib
import re
import shlex
import time

SNAP_LIMIT = 12000

# Lines of the AI snapshot that carry structure but nothing to read or act on.
_NOISE = re.compile(r"^\s*- (generic|group|none|presentation|list|listitem|figure|status|alert|region|toolbar)"
                    r"( \[ref=\w+\])?( \[cursor=pointer\])?:?$")
# A child line that only repeats words already in its parent's name.
_ECHO = re.compile(r'^\s*- (generic|text|paragraph)( \[ref=\w+\])?: "?([^"]*)"?$')
_URL = re.compile(r"^\s*- /url: ")
_OPTION = re.compile(r"^(\s*)- option ")
_REF = re.compile(r"\[ref=(f?\d*e\d+)\]")
_NAME = re.compile(r'^\s*- \w+ "([^"]*)"')
_INTERACTIVE = re.compile(r"^\s*- (button|link|textbox|searchbox|combobox|checkbox|radio|switch|slider|"
                          r"spinbutton|option|menuitem|menuitemcheckbox|menuitemradio|tab|treeitem)\b")


# --- what the model reads -----------------------------------------------------

def compact(snapshot: str, *, full: bool = False) -> str:
    """Drop container noise and long option lists; redact protected values."""
    from .application_checkpoints import PROTECTED
    out, options, opt_indent = [], 0, None
    for line in snapshot.splitlines():
        m = _OPTION.match(line)
        if m and not full:
            if opt_indent == m.group(1):
                options += 1
                if options > 8:
                    continue
            else:
                opt_indent, options = m.group(1), 1
        else:
            if options > 8:
                out.append(f"{opt_indent}- … {options - 8} more options")
            opt_indent, options = None, 0
        if not full and (_NOISE.match(line) or _URL.match(line)):
            continue
        echo = _ECHO.match(line)
        if not full and echo and out and echo.group(3).strip() and echo.group(3).strip() in out[-1]:
            continue
        name = _NAME.match(line)
        if name and PROTECTED.search(name.group(1)) and re.search(r"\]: .+$", line):
            line = re.sub(r"\]: .+$", "]: (protected: the user types this)", line)
        out.append(line)
    if options > 8:
        out.append(f"{opt_indent}- … {options - 8} more options")
    return "\n".join(out)


def interactive(snapshot: str) -> str:
    """Only what can be acted on: one flat line per control, refs kept."""
    return "\n".join(line.strip() for line in snapshot.splitlines()
                     if _INTERACTIVE.match(line) and _REF.search(line))


DIALOG = "[role=dialog]:visible, [role=alertdialog]:visible, dialog[open]"


async def snapshot(page, *, scope: str | None = None, full: bool = False, whole: bool = False,
                   find: str | None = None, controls: bool = False, limit: int = SNAP_LIMIT) -> str:
    """The page, or its open dialog, as an accessibility tree with refs.

    A modal is what the user is looking at, so when one is open only it is
    shown and the page behind it is summarised as one line. `scope` is a ref
    to snapshot one subtree instead, `whole` shows the page even behind a
    dialog, `find` keeps only matching lines and `controls` only the
    elements that can be acted on.
    """
    note = ""
    tree = await page.aria_snapshot(mode="ai")      # issues refs; they stay stable
    if scope:
        tree = await page.locator(f"aria-ref={scope}").aria_snapshot(mode="ai")
    elif not whole and not find:
        dialogs = page.locator(DIALOG)
        if await dialogs.count():
            tree = await dialogs.last.aria_snapshot(mode="ai")
            note = "[a dialog is open; only it is shown. `snap --page` shows everything]\n"
    text = compact(tree, full=full)
    if find:
        hits = [line for line in text.splitlines() if find.lower() in line.lower()]
        return "\n".join(hits) or f"nothing matching {find!r}"
    if controls:
        text = interactive(text) or "no controls on this page"
    text = note + text
    if len(text) > limit:
        text = text[:limit] + (f"\n[snapshot cut at {limit} characters: use `snap --controls`, "
                               "`--find TEXT` or `--scope <ref>`]")
    return text


# --- steps --------------------------------------------------------------------

VERBS = {"click", "dblclick", "hover", "fill", "type", "select", "check", "uncheck", "pick",
         "press", "upload", "drag", "wait", "goto", "back", "reload", "scroll", "viewport",
         "dialog", "snap"}
_NO_REF = {"wait", "goto", "back", "reload", "scroll", "viewport", "dialog", "snap", "press"}
STEP_HELP = ("click e7 | dblclick e7 | hover e7 | fill e4 TEXT | type e4 TEXT | pick e4 TEXT [OPTION] | "
             "select e5 LABEL | check e6 | uncheck e6 | upload e8 PATH | drag e3 e9 | press [e4] KEY | "
             "wait MS|TEXT|gone TEXT|url PART|idle | goto URL | back | reload | scroll [DY|e7] | "
             "viewport WxH | dialog accept|dismiss | snap")


def parse(steps: list[str]) -> list[list[str]]:
    out = []
    for raw in steps:
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            words = shlex.split(line)
            verb = words[0].lower()
            if verb not in VERBS:
                raise ValueError(f"unknown step {verb!r}; steps are {', '.join(sorted(VERBS))}")
            words = [w[1:] if w.startswith("@") and _is_ref(w[1:]) else w for w in words]
            out.append([verb, *words[1:]])
    return out


def _is_ref(word: str) -> bool:
    return bool(re.fullmatch(r"f?\d*e\d+", word or ""))


async def _label(loc) -> str:
    return await loc.evaluate(timeout=5000, expression="""e => (e.getAttribute('aria-label') || e.innerText || e.value
        || e.getAttribute('title') || '').replace(/\\s+/g, ' ').trim().slice(0, 120)""")


async def _protected(loc) -> bool:
    from .application_checkpoints import PROTECTED
    meta = await loc.evaluate(timeout=5000, expression="""e => [e.type, e.id, e.name, e.getAttribute('aria-label'),
        e.getAttribute('placeholder'), e.labels ? [...e.labels].map(x => x.innerText).join(' ') : '']
        .join(' ')""")
    return bool(PROTECTED.search(meta or "")) or "password" in (meta or "").lower()


async def quiet(page, ms: int = 2500) -> None:
    """Wait until the network settles or `ms` passes, whichever is first.

    Single-page apps never go fully idle, so this is a ceiling, not a promise.
    """
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=ms)


async def _safe_eval(frame, js):
    try:
        return await frame.evaluate(js) or 0
    except Exception:                                         # noqa: BLE001
        return 0


async def guard_click(page, label: str, approval: dict | None = None, guard=None) -> None:
    """Refuse a click that would submit, unless a tapped approval covers it."""
    from . import browse
    if approval and approval_covers(approval, label) and page_allowed(approval, page.url):
        approval["used"] = label
        return
    filled = 0
    for fr in page.frames:
        filled += await _safe_eval(fr, browse.FILLED_JS)
    (guard or browse.refuse_submit)(label, filled)


async def guard_enter(page) -> None:
    from . import browse
    for fr in page.frames:
        if await _safe_eval(fr, browse.ENTER_SUBMITS_JS):
            browse.refuse_submit("submit (Enter in a form)")


async def run_steps(page, steps: list[list[str]], *, approval: dict | None = None,
                    guard=None, state=None) -> list[str]:
    """Run steps in order and stop at the first failure.

    `guard(label, filled)` refuses a click that would submit; `approval` is a
    verified approval that lifts that refusal for one matching label. `state`
    is the daemon's record of the tab, where `dialog` arms the next dialog.
    """
    log = []
    for verb, *args in steps:
        ref = args[0] if args and _is_ref(args[0]) else None
        if verb not in _NO_REF and not ref:
            log.append(f"failed at `{verb} {' '.join(args)}`: {verb} needs a ref from `snap`, like e7")
            raise StepError(log)
        loc = page.locator(f"aria-ref={ref}") if ref else None
        try:
            if verb in ("click", "dblclick"):
                label = await _label(loc)
                await guard_click(page, label, approval, guard)
                await (loc.click(timeout=8000) if verb == "click" else loc.dblclick(timeout=8000))
                await quiet(page)
                log.append(f"clicked {ref} {label[:60]!r}")
            elif verb == "hover":
                await loc.hover(timeout=8000)
                log.append(f"hovering over {ref}")
            elif verb in ("fill", "type", "pick"):
                if await _protected(loc):
                    raise SystemExit(f"refused: {ref} is a protected field the user types "
                                     "through `herald browse login`")
                value = args[1]
                if verb == "type":
                    await loc.press_sequentially(value, delay=20)
                else:
                    await loc.fill(value, timeout=8000)
                if verb == "pick":
                    want = args[2] if len(args) > 2 else value
                    option = page.get_by_role("option", name=want, exact=False).first
                    await option.wait_for(timeout=6000)
                    chosen = await _label(option)
                    await option.click(timeout=6000)
                    log.append(f"picked {chosen[:60]!r} in {ref}")
                else:
                    log.append(f"filled {ref}")
            elif verb == "select":
                await loc.select_option(label=args[1], timeout=8000)
                log.append(f"selected {args[1]!r} in {ref}")
            elif verb in ("check", "uncheck"):
                try:
                    await loc.set_checked(verb == "check", timeout=4000)
                except Exception:
                    # A styled radio hides its input behind a label; clicking
                    # the element the snapshot names is what a person does.
                    await loc.click(timeout=4000, force=True)
                log.append(f"{verb}ed {ref}")
            elif verb == "upload":
                try:
                    await loc.set_input_files(args[1], timeout=6000)
                except Exception:
                    # Not the file input itself but the button in front of it.
                    async with page.expect_file_chooser(timeout=6000) as chooser:
                        await loc.click(timeout=6000)
                    await (await chooser.value).set_files(args[1])
                await quiet(page)
                log.append(f"attached {args[1]} to {ref}")
            elif verb == "drag":
                await loc.drag_to(page.locator(f"aria-ref={args[1]}"), timeout=8000)
                log.append(f"dragged {ref} onto {args[1]}")
            elif verb == "press":
                key = args[1] if ref and len(args) > 1 else args[0]
                if ref:
                    await loc.focus()
                if key.lower() == "enter":
                    await guard_enter(page)
                await (loc.press(key) if ref else page.keyboard.press(key))
                await quiet(page)
                log.append(f"pressed {key}")
            elif verb == "wait":
                what = args[0] if args else "1000"
                if what.isdigit():
                    await page.wait_for_timeout(int(what))
                elif _is_ref(what):
                    await page.locator(f"aria-ref={what}").wait_for(timeout=10000)
                elif what == "idle":
                    await quiet(page, 10000)
                elif what == "gone" and len(args) > 1:
                    await page.get_by_text(args[1], exact=False).first.wait_for(state="hidden", timeout=15000)
                elif what == "url" and len(args) > 1:
                    await page.wait_for_url(lambda u: args[1] in u, timeout=15000)
                else:
                    text = args[1] if what == "text" and len(args) > 1 else what
                    await page.get_by_text(text, exact=False).first.wait_for(timeout=15000)
                log.append(f"waited {' '.join(args)}")
            elif verb == "goto":
                await page.goto(args[0], wait_until="domcontentloaded", timeout=60000)
                await quiet(page, 4000)
                log.append(f"at {page.url}")
            elif verb in ("back", "reload"):
                await (page.go_back(timeout=30000) if verb == "back" else page.reload(timeout=60000))
                await quiet(page, 4000)
                log.append(f"at {page.url}")
            elif verb == "scroll":
                if ref:
                    await loc.scroll_into_view_if_needed(timeout=6000)
                    log.append(f"scrolled to {ref}")
                else:
                    await page.mouse.wheel(0, int(args[0]) if args else 800)
                    await page.wait_for_timeout(300)
                    log.append("scrolled")
            elif verb == "viewport":
                w, h = (int(n) for n in args[0].lower().split("x"))
                await page.set_viewport_size({"width": w, "height": h})
                log.append(f"viewport {w}x{h}")
            elif verb == "dialog":
                if state is not None:
                    state.dialog = args[0].lower() if args else "accept"
                log.append(f"the next dialog will be {args[0].lower() if args else 'accept'}ed")
            elif verb == "snap":
                log.append(await snapshot(page))
        except SystemExit as exc:
            log.append(f"stopped at `{verb} {' '.join(args)}`: {exc}")
            raise StepError(log) from None
        except StepError:
            raise
        except Exception as exc:                              # noqa: BLE001
            msg = str(exc).splitlines()[0][:300]
            if ref and "Timeout" in msg and verb not in ("wait", "goto"):
                msg = (f"{ref} is not on the page any more (refs go stale when the page "
                       "changes); take a new `snap`")
            log.append(f"failed at `{verb} {' '.join(args)}`: {msg}")
            raise StepError(log) from None
    return log


class StepError(Exception):
    def __init__(self, log):
        super().__init__("\n".join(log))
        self.log = log


# --- approvals bound to one click ----------------------------------------------

def approval_for(question_id: int, covers: str) -> dict:
    """A tapped approval whose text contains `covers` verbatim.

    The approval was for an exact action written out in full; `covers` quotes
    the part of it this click carries out, and the button's label has to be
    found in that quote. Approvals older than two hours do not count.
    """
    from . import questions
    row = questions.result(question_id)
    if not row.get("approved"):
        raise SystemExit(f"approval {question_id} was not approved; nothing done")
    answered = row.get("answered_at") or 0
    if isinstance(answered, str):
        from datetime import datetime
        answered = datetime.fromisoformat(answered).timestamp()
    standing = standing_terms(row["question"])
    if standing:
        until, pages = standing
        if float(answered) > until or until - float(answered) > 7 * 86400:
            raise SystemExit(f"approval {question_id}: a standing approval may last at most seven days")
        if time.time() > until:
            raise SystemExit(f"standing approval {question_id} ended; ask again")
    elif time.time() - float(answered) > 7200:
        raise SystemExit(f"approval {question_id} is more than two hours old; ask again")
    norm = lambda s: re.sub(r"\s+", " ", s or "").strip().lower()   # noqa: E731
    if not covers or norm(covers) not in norm(row["question"]):
        raise SystemExit("--covers must quote the approved text exactly")
    return {"id": question_id, "approval_id": row.get("approval_id"),
            "covers": covers, "used": None, "pages": standing[1] if standing else None}


def standing_terms(text: str) -> tuple[float, list[str]] | None:
    """A standing approval names when it ends and the pages it covers:

        Standing until: 2026-11-22T11:00:00-05:00
        Pages: https://example.org/a/, https://example.org/b/

    It then covers repeated clicks the quote allows, on those pages only,
    until that time, instead of one click within two hours. Without both
    lines it is an ordinary one-click approval."""
    from datetime import datetime
    m = re.search(r"^\s*Standing until:\s*(\S+)\s*$", text or "", re.M | re.I)
    p = re.search(r"^\s*Pages:\s*(.+?)\s*$", text or "", re.M | re.I)
    if not m or not p:
        return None
    try:
        until = datetime.fromisoformat(m.group(1)).timestamp()
    except ValueError:
        return None
    pages = [u.strip() for u in p.group(1).split(",") if u.strip().startswith("https://")]
    return (until, pages) if pages else None


def page_allowed(approval: dict, url: str) -> bool:
    pages = approval.get("pages")
    return pages is None or any(url.startswith(prefix) for prefix in pages)


def approval_covers(approval: dict, label: str) -> bool:
    """Every word of the button's label appears in the quoted approved text,
    the label is not an application submit, and nothing used it yet."""
    if approval.get("used"):
        return False
    if re.search(r"\b(apply|application)\b", label or "", re.I):
        return False                       # job applications are never submitted
    words = re.findall(r"[a-z0-9]+", (label or "").lower())
    text = (approval.get("covers") or "").lower()
    return bool(words) and all(re.search(rf"\b{re.escape(w)}", text) for w in words)


def record_approved_click(approval: dict, url: str) -> None:
    from . import db
    with db.session() as con:
        db.record_action(con, actor="session", tier="red", kind="browse.click",
                         target=url, summary=f"clicked {approval['used']!r} under approval "
                         f"question {approval['id']}", ref=str(approval["id"]),
                         approval_id=approval["approval_id"])
