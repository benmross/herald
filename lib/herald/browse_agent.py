"""Element refs and batched steps for `herald browse`, over one held connection.

Why this exists: on 13 Nov 2026 a LinkedIn profile edit took about seventy
tool calls. Every `herald browse` command was its own process, so it
reconnected, re-tagged fields (handles shifted between calls) and slept a
guessed time. Clicking "by text" hit a paragraph instead of the button next
to it. Radios, checkboxes and rich-text boxes were missing from `fields`. A
form defaulting to "replace your headline" was nearly missed for that reason.

What other agent browsers do, surveyed that night: Vercel's agent-browser and
Microsoft's Playwright CLI both keep a browser connection alive in a daemon,
print the page as an accessibility tree with a short ref per element (`e7`),
act on refs, and let several steps run in one call. Both read refs from
Playwright's own AI snapshot (`aria_snapshot(mode="ai")` and the `aria-ref=`
selector), which Herald's Playwright already ships. Herald uses that engine
directly rather than either CLI because they would sit beside Herald's guards
instead of under them. Those guards are the workspace leases, the submit
refusal, protected fields, application checkpoints and the page-content
label. Using the engine also avoids another runtime on the machine.

Refs live in the Playwright connection that took the snapshot: a second
process connecting over CDP cannot resolve them (checked 13 Nov 2026). So one
small server per workspace holds the connection and answers the CLI over a
unix socket. It takes the workspace lease per request, not for its lifetime,
so the older one-shot commands keep working alongside it. It exits after
`IDLE_SECONDS` without a request.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time

from . import config

IDLE_SECONDS = 600
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


def sock_path(workspace: str) -> str:
    folder = config.HOME / "run"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return str(folder / f"browse-{workspace}.sock")


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


DIALOG = "[role=dialog]:visible, [role=alertdialog]:visible, dialog[open]"


def snapshot(page, *, scope: str | None = None, full: bool = False,
             limit: int = SNAP_LIMIT) -> str:
    """The page, or its open dialog, as an accessibility tree with refs.

    A modal is what the user is looking at, so when one is open only it is
    shown and the page behind it is summarised as one line. `scope` is a ref
    to snapshot one subtree instead.
    """
    note = ""
    tree = page.aria_snapshot(mode="ai")      # issues refs; they stay stable
    if scope:
        tree = page.locator(f"aria-ref={scope}").aria_snapshot(mode="ai")
    else:
        dialogs = page.locator(DIALOG)
        if dialogs.count():
            tree = dialogs.last.aria_snapshot(mode="ai")
            note = "[a dialog is open; only it is shown. `snap --page` shows everything]\n"
    text = note + compact(tree, full=full)
    if len(text) > limit:
        text = text[:limit] + f"\n[snapshot cut at {limit} characters: use `snap --scope <ref>` or `find`]"
    return text


# --- steps --------------------------------------------------------------------

VERBS = {"click", "fill", "type", "select", "check", "uncheck", "pick", "press",
         "wait", "goto", "scroll", "snap"}


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
            if words[1:2] and words[1].startswith("@"):
                words[1] = words[1][1:]
            out.append([verb, *words[1:]])
    return out


def _label(loc) -> str:
    return loc.evaluate(timeout=5000, expression="""e => (e.getAttribute('aria-label') || e.innerText || e.value
        || e.getAttribute('title') || '').replace(/\\s+/g, ' ').trim().slice(0, 120)""")


def _protected(loc) -> bool:
    from .application_checkpoints import PROTECTED
    meta = loc.evaluate(timeout=5000, expression="""e => [e.type, e.id, e.name, e.getAttribute('aria-label'),
        e.getAttribute('placeholder'), e.labels ? [...e.labels].map(x => x.innerText).join(' ') : '']
        .join(' ')""")
    return bool(PROTECTED.search(meta or "")) or "password" in (meta or "").lower()


def quiet(page, ms: int = 2500) -> None:
    """Wait until the network settles or `ms` passes, whichever is first.

    Single-page apps never go fully idle, so this is a ceiling, not a promise.
    """
    with contextlib.suppress(Exception):
        page.wait_for_load_state("networkidle", timeout=ms)


def run_steps(page, steps: list[list[str]], *, approval: dict | None = None,
              guard=None) -> list[str]:
    """Run steps in order and stop at the first failure.

    `guard(label)` refuses a click that would submit; `approval` is a
    verified approval that lifts that refusal for one matching label.
    """
    from . import browse
    log = []
    for verb, *args in steps:
        ref = args[0] if args else None
        loc = page.locator(f"aria-ref={ref}") if ref and verb not in ("wait", "goto", "scroll", "snap") else None
        try:
            if verb == "click":
                label = _label(loc)
                if approval and approval_covers(approval, label):
                    approval["used"] = label
                else:
                    filled = sum(_safe_eval(fr, browse.FILLED_JS) for fr in page.frames)
                    (guard or browse.refuse_submit)(label, filled)
                loc.click(timeout=8000)
                quiet(page)
                log.append(f"clicked {ref} {label[:60]!r}")
            elif verb in ("fill", "type", "pick"):
                if _protected(loc):
                    raise SystemExit(f"refused: {ref} is a protected field the user types "
                                     "through `herald browse login`")
                value = args[1]
                if verb == "type":
                    loc.press_sequentially(value, delay=20)
                else:
                    loc.fill(value, timeout=8000)
                if verb == "pick":
                    want = args[2] if len(args) > 2 else value
                    option = page.get_by_role("option", name=want, exact=False).first
                    option.wait_for(timeout=6000)
                    chosen = _label(option)
                    option.click(timeout=6000)
                    log.append(f"picked {chosen[:60]!r} in {ref}")
                else:
                    log.append(f"filled {ref}")
            elif verb == "select":
                loc.select_option(label=args[1], timeout=8000)
                log.append(f"selected {args[1]!r} in {ref}")
            elif verb in ("check", "uncheck"):
                try:
                    loc.set_checked(verb == "check", timeout=4000)
                except Exception:
                    # A styled radio hides its input behind a label; clicking
                    # the element the snapshot names is what a person does.
                    loc.click(timeout=4000, force=True)
                log.append(f"{verb}ed {ref}")
            elif verb == "press":
                key = args[1] if len(args) > 1 else args[0]
                target = loc if len(args) > 1 else page.keyboard
                if key.lower() == "enter":
                    if len(args) > 1:
                        loc.focus()
                    for fr in page.frames:
                        if _safe_eval(fr, browse.ENTER_SUBMITS_JS):
                            browse.refuse_submit("submit (Enter in a form)")
                (target.press(key) if len(args) > 1 else page.keyboard.press(key))
                quiet(page)
                log.append(f"pressed {key}")
            elif verb == "wait":
                what = args[0] if args else "1000"
                if what.isdigit():
                    page.wait_for_timeout(int(what))
                elif what == "text":
                    page.get_by_text(args[1], exact=False).first.wait_for(timeout=10000)
                else:
                    page.get_by_text(what, exact=False).first.wait_for(timeout=10000)
                log.append(f"waited {' '.join(args)}")
            elif verb == "goto":
                page.goto(args[0], wait_until="domcontentloaded", timeout=60000)
                quiet(page, 4000)
                log.append(f"at {page.url}")
            elif verb == "scroll":
                page.mouse.wheel(0, int(args[0]) if args else 800)
                log.append("scrolled")
            elif verb == "snap":
                log.append(snapshot(page))
        except SystemExit as exc:
            log.append(f"stopped at `{verb} {' '.join(args)}`: {exc}")
            raise StepError(log) from None
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


def _safe_eval(frame, js):
    try:
        return frame.evaluate(js) or 0
    except Exception:                                         # noqa: BLE001
        return 0


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
    if time.time() - float(answered) > 7200:
        raise SystemExit(f"approval {question_id} is more than two hours old; ask again")
    norm = lambda s: re.sub(r"\s+", " ", s or "").strip().lower()   # noqa: E731
    if not covers or norm(covers) not in norm(row["question"]):
        raise SystemExit("--covers must quote the approved text exactly")
    return {"id": question_id, "approval_id": row.get("approval_id"),
            "covers": covers, "used": None}


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


# --- the server -----------------------------------------------------------------

class Server:
    """Holds one connection to a workspace's browser. One request at a time."""

    def __init__(self, workspace: str):
        from . import browse, browser_workspaces as bw
        from playwright.sync_api import sync_playwright
        self.workspace = bw.name(workspace)
        self._pw = sync_playwright().start()
        self.session = None
        self.browse = browse

    def _connect(self):
        """A Session without its own lease, reused while the browser lives."""
        from . import browse
        if self.session is not None:
            try:
                if self.session.browser.is_connected():
                    return self.session
            except Exception:                                 # noqa: BLE001
                pass
        s = browse.Session.__new__(browse.Session)
        s.workspace, s.human = self.workspace, False
        s._accounts, s._claimed_hosts = contextlib.ExitStack(), set()
        s._lease, s._pw, s._owns_playwright = None, self._pw, False
        browse.ensure(self.workspace)
        s.browser = self._pw.chromium.connect_over_cdp(browse.cdp_url(self.workspace))
        s.ctx = s.browser.contexts[0] if s.browser.contexts else s.browser.new_context()
        self.session = s
        return s

    def handle(self, req: dict) -> dict:
        from . import browser_workspaces as bw
        with bw.lease(self.workspace, timeout=req.get("timeout", 8)):
            s = self._connect()
            s._accounts, s._claimed_hosts = contextlib.ExitStack(), set()
            try:
                page = s.tab(req["tab"])
                s.claim_account(page.url)
                cmd = req["cmd"]
                if cmd == "snap":
                    if req.get("find"):
                        tree = compact(page.aria_snapshot(mode="ai"))
                        hits = [l for l in tree.splitlines() if req["find"].lower() in l.lower()]
                        body = "\n".join(hits) or f"nothing matching {req['find']!r}"
                    elif req.get("page"):
                        body = compact(page.aria_snapshot(mode="ai"), full=req.get("full", False))
                        if len(body) > SNAP_LIMIT:
                            body = body[:SNAP_LIMIT] + "\n[cut: use --find or --scope]"
                    else:
                        body = snapshot(page, scope=req.get("scope"), full=req.get("full", False))
                    return {"ok": True, "out": f"{page.url}\n{self.browse.UNTRUSTED}\n{body}"}
                if cmd == "do":
                    approval = None
                    if req.get("approval"):
                        approval = approval_for(int(req["approval"]), req.get("covers") or "")
                    s.checkpoint_page(page, "before-browser-command")
                    steps = parse(req["steps"])
                    if not any(v == "snap" for v, *_ in steps) and not req.get("quiet"):
                        steps.append(["snap"])
                    try:
                        log = run_steps(page, steps, approval=approval)
                        ok = True
                    except StepError as e:
                        log, ok = e.log, False
                        with contextlib.suppress(Exception):
                            log.append(snapshot(page))
                    if approval and approval.get("used"):
                        _record_approved_click(approval, page.url)
                    head = f"{page.url}\n{self.browse.UNTRUSTED}\n"
                    return {"ok": ok, "out": head + "\n".join(log)}
                raise ValueError(f"unknown command {cmd!r}")
            finally:
                s._accounts.close()

    def serve(self):
        path = sock_path(self.workspace)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        os.chmod(path, 0o600)
        srv.listen(4)
        srv.settimeout(IDLE_SECONDS)
        try:
            while True:
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    return
                with conn:
                    try:
                        req = json.loads(_recv(conn))
                        resp = self.handle(req)
                    except SystemExit as e:
                        resp = {"ok": False, "out": str(e)}
                    except Exception as e:                    # noqa: BLE001
                        resp = {"ok": False, "out": f"{type(e).__name__}: {str(e).splitlines()[0][:400]}"}
                    conn.sendall(json.dumps(resp).encode() + b"\n")
        finally:
            with contextlib.suppress(Exception):
                os.unlink(path)
            with contextlib.suppress(Exception):
                self._pw.stop()


def _record_approved_click(approval: dict, url: str) -> None:
    from . import db
    with db.session() as con:
        db.record_action(con, actor="session", tier="red", kind="browse.click",
                         target=url, summary=f"clicked {approval['used']!r} under approval "
                         f"question {approval['id']}", ref=str(approval["id"]),
                         approval_id=approval["approval_id"])


def _recv(conn) -> str:
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
    return buf.decode()


# --- the client -------------------------------------------------------------------

def request(workspace: str, req: dict, *, timeout: float = 180) -> dict:
    """Send one request, starting the workspace's server if none is running."""
    from . import browser_workspaces as bw
    workspace = bw.name(workspace)
    path = sock_path(workspace)
    c = _connect(path, timeout)
    if c is None:
        # No server, or a stale socket left by one that was killed.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        _spawn(workspace)
        deadline = time.monotonic() + 30
        while c is None and time.monotonic() < deadline:
            time.sleep(0.15)
            c = _connect(path, timeout)
        if c is None:
            raise SystemExit("the browse server did not start; see "
                             f"{config.HOME / 'browser' / 'agent.log'}")
    with c:
        c.sendall(json.dumps(req).encode() + b"\n")
        return json.loads(_recv(c))


def _connect(path: str, timeout: float):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(timeout)
    try:
        c.connect(path)
        return c
    except (FileNotFoundError, ConnectionRefusedError):
        c.close()
        return None


def _spawn(workspace: str) -> None:
    log = config.HOME / "browser" / "agent.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(config.ROOT / "lib") + os.pathsep + env.get("PYTHONPATH", "")
    with open(log, "a") as out:
        subprocess.Popen([sys.executable, "-m", "herald.browse_agent", "serve", workspace],
                         stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                         start_new_session=True, cwd=str(config.ROOT), env=env)


if __name__ == "__main__":
    if sys.argv[1:2] == ["serve"]:
        Server(sys.argv[2] if len(sys.argv) > 2 else "default").serve()
