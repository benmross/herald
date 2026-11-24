"""The browse daemon: the one Playwright client of Herald's browsers.

Every `herald browse` command used to be its own process that started
Playwright, attached to the shared browser, did one thing and left. Three
things followed from that, and all three hurt most when several agents
worked at once:

- attaching waits on every tab, so one bad tab hung every command
  (see browse_cdp.py for the outage this caused on 20 Nov 2026);
- a command could only be kept apart from another by locking the whole
  browser, so agents queued behind each other and failed as busy;
- nothing outlived a command, so nothing could own a tab, notice it was
  abandoned, or remember what the page logged.

This process holds the connections instead and answers requests on a unix
socket (`$HERALD_HOME/run/browse.sock`): one JSON line in, one out. It is
asyncio throughout, so commands on different tabs run at the same time and
two commands on one tab take turns.

What it keeps:

- one connection per profile browser (`default`, a worker, a remote), made
  on first use, bounded, and remade when the browser restarts;
- one headless **clean** browser with a separate context per session, for
  work that needs no sign-in: parallel agents get separate cookies, a
  device or viewport of their own, and nothing they do shows on the screen
  a person may be signing in on;
- per tab: its owner, a lock, and what the page said (console, requests,
  dialogs, downloads, popups);
- a janitor pass every minute over `browse.heal`, and a restart of a
  browser that has stopped answering when nothing unfinished lives in it.

Run by `herald-browse-daemon.service` where systemd is used; otherwise the
first command starts it and it exits after half an hour idle.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import fcntl
import json
import os
import pathlib
import re
import signal
import sys
import time

from . import config

IDLE_EXIT = 30 * 60
JANITOR_SECONDS = 60
TEXT_LIMIT = 12000
STATIC = {"image", "stylesheet", "font", "media", "manifest", "ping"}
ACTS = re.compile(r"\.\s*(click|submit|requestSubmit|dispatchEvent)\s*\(|sendBeacon|"
                  r"method\s*:\s*['\"`](post|put|patch|delete)", re.I)


def sock_path() -> str:
    folder = config.HOME / "run"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return str(folder / "browse.sock")


class Refused(Exception):
    """A request that was understood and declined. The message is the reply."""


class Tab:
    """What the daemon remembers about one tab while it is open."""

    def __init__(self, workspace, tab, target, page, session):
        self.workspace, self.tab, self.target, self.page, self.session = workspace, tab, target, page, session
        self.lock = asyncio.Lock()
        self.console = collections.deque(maxlen=200)
        self.requests = collections.deque(maxlen=400)
        self.notes: list[str] = []
        self.jobs: set = set()
        self.dialog: str | None = None
        self.n = 0


class Conn:
    def __init__(self, workspace, browser, ctx, cdp):
        self.workspace, self.browser, self.ctx, self.cdp = workspace, browser, ctx, cdp
        self.seen: dict = {}


class Daemon:
    def __init__(self):
        self.pw = None
        self.conns: dict[str, Conn] = {}
        self.conn_locks = collections.defaultdict(asyncio.Lock)
        self.clean = None
        self.clean_renderer: str | None = None
        self.clean_lock = asyncio.Lock()
        self.contexts: dict[tuple, object] = {}
        self.clean_seen: dict = {}
        self.tabs: dict[tuple, Tab] = {}
        self.started = self.last_request = time.time()
        self.active = 0
        self.health: dict[str, dict] = {}
        self.failures = collections.Counter()
        self.stop = asyncio.Event()

    # --- browsers ----------------------------------------------------------------

    async def conn(self, workspace: str) -> Conn:
        from . import browse, browse_cdp, browser_workspaces as bw
        async with self.conn_locks[workspace]:
            c = self.conns.get(workspace)
            if c is not None and c.browser.is_connected():
                return c

            def prepare():
                browse.ensure(workspace)
                version = browse_cdp.http_json(browse._port(workspace), "/json/version", 3)
                bw.note_browser(workspace, version["webSocketDebuggerUrl"].rsplit("/", 1)[-1])
            await asyncio.to_thread(prepare)
            browser = await browse.aconnect(self.pw, workspace)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            c = Conn(workspace, browser, ctx, await browser.new_browser_cdp_session())
            self.conns[workspace] = c
            for key in [k for k in self.tabs if k[0] == workspace]:
                del self.tabs[key]              # page objects of the old connection
            return c

    async def clean_browser(self):
        from . import browse, browser_workspaces as bw
        async with self.clean_lock:
            if self.clean is None or not self.clean.is_connected():
                await asyncio.to_thread(bw.admit)
                self.clean = await self.pw.chromium.launch(
                    headless=True, executable_path=config.get("browser.executable") or None,
                    args=["--disable-blink-features=AutomationControlled"] + browse.gpu_args())
                self.clean_renderer = None
                self.contexts, self.clean_seen = {}, {}
                for key in [k for k in self.tabs if k[0] == "clean"]:
                    del self.tabs[key]
            return self.clean

    async def clean_webgl(self) -> str:
        """What the clean browser draws WebGL with, asked of the browser itself."""
        from . import browse
        wanted = "GPU requested" if browse.gpu_args() else f"software (browser.gpu is {browse.gpu_mode()})"
        if self.clean is None or not self.clean.is_connected():
            return f"{wanted}, browser not started"
        if self.clean_renderer is None:
            ctx = await self.clean.new_context()
            try:
                page = await ctx.new_page()
                self.clean_renderer = await page.evaluate("""() => {
                    const gl = document.createElement('canvas').getContext('webgl2');
                    if (!gl) return 'no WebGL';
                    const e = gl.getExtension('WEBGL_debug_renderer_info');
                    return e ? gl.getParameter(e.UNMASKED_RENDERER_WEBGL) : 'unknown renderer';
                }""")
            except Exception as error:
                return f"{wanted}, could not ask ({type(error).__name__})"
            finally:
                await ctx.close()
        software = any(word in self.clean_renderer.lower() for word in ("swiftshader", "llvmpipe", "no webgl"))
        return f"{'software' if software else 'GPU'} ({self.clean_renderer})"

    async def clean_context(self, session: str, device: str | None):
        from . import browse
        browser = await self.clean_browser()
        key = (session, device or "")
        ctx = self.contexts.get(key)
        if ctx is None:
            opts = {"user_agent": browse.DESKTOP_UA, "viewport": {"width": 1280, "height": 900}}
            if device:
                found = next((d for name, d in self.pw.devices.items() if name.lower() == device.lower()), None)
                if found is None:
                    near = [n for n in self.pw.devices if device.lower().split()[0] in n.lower()][:8]
                    raise Refused(f"no device named {device!r}"
                                  + (f"; close matches: {', '.join(near)}" if near else
                                     "; examples: iPhone 15, Pixel 7, iPad Pro 11"))
                opts = {k: v for k, v in found.items() if k != "default_browser_type"}
            ctx = await browser.new_context(accept_downloads=True, **opts)
            self.contexts[key] = ctx
        return ctx

    # --- tabs --------------------------------------------------------------------

    async def pages(self, workspace: str) -> dict:
        """Short id -> page, for every open page of a workspace."""
        from . import browse
        if workspace == "clean":
            return {t.tab: t.page for k, t in self.tabs.items() if k[0] == "clean" and not t.page.is_closed()}
        c = await self.conn(workspace)
        pages = [p for p in c.ctx.pages if not p.url.startswith("devtools://")]
        targets = await asyncio.gather(*[browse.atarget(c.ctx, p, c.seen) for p in pages])
        return {t[:6].lower(): p for p, t in zip(pages, targets) if t}

    def track(self, workspace, tab, target, page, session) -> Tab:
        key = (workspace, tab)
        st = self.tabs.get(key)
        if st is not None and st.page is page:
            return st
        st = self.tabs[key] = Tab(workspace, tab, target, page, session)
        self.listen(st)
        return st

    def listen(self, st: Tab) -> None:
        from . import browse, browse_tabs as tabs
        page = st.page

        def console(msg):
            st.console.append((time.time(), msg.type, msg.text[:600]))

        def page_error(exc):
            st.console.append((time.time(), "pageerror", str(exc)[:600]))

        def request_done(response):
            req = response.request
            if req.resource_type in STATIC and response.status < 400:
                return
            st.n += 1
            st.requests.append({"n": st.n, "method": req.method, "url": response.url, "status": response.status,
                                "type": req.resource_type, "response": response})

        def request_failed(req):
            st.n += 1
            st.requests.append({"n": st.n, "method": req.method, "url": req.url, "status": None,
                                "type": req.resource_type, "failure": req.failure, "response": None})

        async def dialog(d):
            action = st.dialog or ("accept" if d.type in ("alert", "beforeunload") else "dismiss")
            st.dialog = None
            st.notes.append(f"[a {d.type} dialog said {d.message[:160]!r} and was {action}ed"
                            + ("" if action == "accept" or d.type == "alert" else
                               "; to accept the next one, put `dialog accept` before the step that opens it") + "]")
            with contextlib.suppress(Exception):
                await (d.accept() if action == "accept" else d.dismiss())

        async def download(dl):
            folder = browse.home() / "downloads" / st.session
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            name = re.sub(r"[^A-Za-z0-9._-]", "_", dl.suggested_filename or "download")[:120]
            path = folder / name
            try:
                await dl.save_as(str(path))
                st.notes.append(f"[downloaded {path}]")
            except Exception as e:                            # noqa: BLE001
                st.notes.append(f"[a download started but was not saved: {str(e).splitlines()[0][:120]}]")

        def start_download(dl):
            job = asyncio.ensure_future(download(dl))
            st.jobs.add(job)
            job.add_done_callback(st.jobs.discard)

        async def popup(new):
            try:
                if st.workspace == "clean":
                    ctx, seen = new.context, self.clean_seen
                else:
                    c = self.conns[st.workspace]
                    ctx, seen = c.ctx, c.seen
                target = await asyncio.wait_for(browse.atarget(ctx, new, seen), 5)
                tab = tabs.claim(st.workspace, target, st.session, url=new.url)
                self.track(st.workspace, tab, target, new, st.session)
                st.notes.append(f"[this opened a new tab {tab}: {new.url[:120] or 'still loading'}]")
            except Exception:                                 # noqa: BLE001
                pass

        def closed(_page=None):
            # Only forget what the daemon knew. The registry row goes when
            # the browser no longer lists the tab (heal's sync): Playwright
            # also reports every page closed when the daemon disconnects,
            # and treating that as closed made a restart orphan every tab.
            self.tabs.pop((st.workspace, st.tab), None)

        page.on("console", console)
        page.on("pageerror", page_error)
        page.on("response", request_done)
        page.on("requestfailed", request_failed)
        page.on("dialog", dialog)
        page.on("download", start_download)
        page.on("popup", popup)
        page.on("close", closed)

    async def resolve(self, req) -> Tab:
        """The tab a request names: an id (anyone's), or among this session's
        own tabs an index or a unique piece of the URL."""
        from . import browse, browse_tabs as tabs
        ref = str(req.get("tab") or "").strip().lower()
        session, workspace = req["session"], req.get("workspace")
        if not ref:
            raise Refused("give a tab id; `herald browse tabs` lists yours")
        is_id = bool(re.fullmatch(r"[0-9a-f]{6}", ref))
        row = tabs.get(ref, workspace) if is_id else None
        if row is None:
            mine = [r for r in tabs.rows(session=session) if not workspace or r["workspace"] == workspace]
            if ref.isdigit() and int(ref) < len(mine):
                row = mine[int(ref)]
            else:
                hits = [r for r in mine if ref in (r["url"] or "").lower()]
                if len(hits) > 1:
                    raise Refused(f"{len(hits)} of this session's tabs match {ref!r}; use a tab id")
                row = hits[0] if hits else None
        page = None
        if row is not None:
            known = self.tabs.get((row["workspace"], row["tab"]))
            if known is not None and not known.page.is_closed():
                return known                    # the usual case: no need to look at any other tab
        if row is None and is_id:
            # Not recorded: opened by another client a moment ago, or older
            # than the registry. Take it as nobody's.
            ws = workspace or "default"
            page = (await self.pages(ws)).get(ref) if ws != "clean" else None
            if page is not None:
                tabs.claim(ws, self.conns[ws].seen[page], "", url=page.url)
                row = tabs.get(ref, ws)
        if row is None:
            raise Refused(f"no tab {ref!r}. `herald browse tabs` lists this session's tabs "
                          "and `herald browse tabs --all` everyone's.")
        if page is None:
            page = (await self.pages(row["workspace"])).get(row["tab"])
        if page is None or page.is_closed():
            tabs.forget(row["workspace"], row["tab"])
            raise Refused(f"tab {row['tab']} has been closed")
        return self.track(row["workspace"], row["tab"], row.get("target") or "", page,
                          row["session"] or session)

    @staticmethod
    def policy(workspace: str, tab: str, url: str) -> bool:
        """Raise if automation may not act in this workspace now; return
        whether the tab holds an unfinished application."""
        from . import apply, browser_workspaces as bw, db
        bw.assert_agent(workspace, url if url.startswith("http") else None)
        try:
            with db.session() as c:
                row = c.execute("SELECT a.status FROM applications a JOIN application_browser b "
                                "ON b.app_id=a.id WHERE b.workspace=? AND b.tab=?", (workspace, tab)).fetchone()
        except Exception:                                     # noqa: BLE001
            return False
        return bool(row and row["status"] not in apply.DONE)

    @staticmethod
    async def flock(path: str, deadline: float, what: str, holder=None, shared: bool = False) -> int:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise Refused(f"{what} is busy" + (f": {holder()}" if holder else "")
                                  + ". Retry when that finishes.") from None
                await asyncio.sleep(0.1)

    async def checkpoint(self, st: Tab, stage: str) -> str | None:
        """Record an application draft around a command. The adapters that
        read a draft are synchronous, so this runs them in their own process."""
        env = dict(os.environ, PYTHONPATH=str(config.ROOT / "lib") + os.pathsep + os.environ.get("PYTHONPATH", ""))
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "herald.browse", "checkpoint", st.workspace, st.tab, stage,
            env=env, cwd=str(config.ROOT), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), 90)
        except asyncio.TimeoutError:
            proc.kill()
            return "timed out"
        return (err.decode(errors="replace").strip().splitlines() or ["failed"])[-1][:200] if proc.returncode else None

    @contextlib.asynccontextmanager
    async def using(self, req, *, mutates: bool = False):
        """Resolve a tab and hold it for one command."""
        from . import browse_tabs as tabs, browser_workspaces as bw
        st = await self.resolve(req)
        row = tabs.get(st.tab, st.workspace)
        if tabs.with_person(row):
            until = time.strftime("%-I:%M %p", time.localtime(row["human_until"]))
            raise Refused(f"tab {st.tab} is with the user ({row['human_why']}) until they tap Done, or "
                          f"{until} when the link expires. Wait with `herald browse returned {st.tab} "
                          "--wait 600`. Every other tab still works.")
        bound = False
        if st.workspace != "clean":
            try:
                bound = await asyncio.to_thread(self.policy, st.workspace, st.tab, st.page.url)
            except bw.Busy as e:
                raise Refused(str(e)) from None
        # Wait for a busy tab, but answer "busy" well before the command's own time runs out.
        deadline = time.monotonic() + min(45.0, float(req.get("timeout") or 180) * 0.6)
        try:
            await asyncio.wait_for(st.lock.acquire(), max(0.1, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            raise Refused(f"tab {st.tab} is busy with another command. Retry when that finishes.") from None
        fds = []
        try:
            if st.workspace != "clean":
                # Shared with every other agent; a person's review holds it alone.
                fds.append(await self.flock(bw.lock_path(st.workspace), time.monotonic() + 8,
                                            "this browser workspace", shared=True))
            fds.append(await self.flock(tabs._lock_path(st.workspace, st.tab), deadline, f"tab {st.tab}",
                                        lambda: tabs.lock_holder(st.workspace, st.tab)))
            if bound:
                fds.append(await self.flock(bw.account_lock_path(st.page.url), time.monotonic() + 8,
                                            "this employer account"))
                if mutates:
                    failed = await self.checkpoint(st, "before-browser-command")
                    if failed:
                        raise Refused(f"this tab holds an application draft that could not be checkpointed "
                                      f"({failed}), so nothing was changed.")
            yield st
            if st.jobs:
                await asyncio.wait(st.jobs, timeout=20)
            if bound and mutates and not st.page.is_closed():
                failed = await self.checkpoint(st, "after-browser-command")
                if failed:
                    st.notes.append(f"[the draft was changed but its checkpoint failed: {failed}]")
        finally:
            for fd in fds:
                with contextlib.suppress(OSError):
                    os.close(fd)
            st.lock.release()
            if not st.page.is_closed():
                if row and not row["session"] and not row["pid"]:
                    tabs.claim(st.workspace, st.target or row["target"] or st.tab, req["session"], url=st.page.url)
                else:
                    tabs.touch(st.workspace, st.tab, st.page.url)

    # --- page helpers ------------------------------------------------------------

    @staticmethod
    async def settle(page, wait_ms: int) -> None:
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=wait_ms)
        with contextlib.suppress(Exception):
            # A script-built page can be quiet and still empty for a moment.
            if len(await page.inner_text("body", timeout=3000)) < 200:
                await page.wait_for_timeout(min(wait_ms, 1000))

    async def goto(self, page, url: str, wait_ms: int) -> None:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:                                # noqa: BLE001
            msg = str(e)
            if "interrupted by another navigation" in msg:
                # A site that redirects by script interrupts goto. The page
                # it went to is the one wanted.
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("domcontentloaded", timeout=45000)
            elif "Download is starting" in msg:
                return
            else:
                raise Refused(f"could not open {url}: {msg.splitlines()[0][:240]}") from None
        await self.settle(page, wait_ms)

    @staticmethod
    async def visible_text(page, selector: str = "body") -> str:
        parts = []
        for fr in page.frames:
            try:
                t = await fr.inner_text(selector, timeout=3000)
            except Exception:                                 # noqa: BLE001
                continue
            if t.strip():
                parts.append(t if fr == page.main_frame else f"\n[frame {fr.url[:90]}]\n{t}")
        return re.sub(r"\n{3,}", "\n\n", "\n".join(parts)).strip()

    @staticmethod
    def clean_url(url: str) -> str:
        url = (url or "").strip()
        if not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
            url = "https://" + url
        if not url.lower().startswith(("http://", "https://", "file://", "about:", "data:")):
            raise Refused(f"refusing to open {url.split(':')[0]}: URLs; use http, https or file")
        return url

    async def head(self, st: Tab) -> str:
        try:
            title = (await st.page.title())[:70]
        except Exception:                                     # noqa: BLE001
            title = ""
        return f"tab {st.tab}  {title}  {st.page.url}"

    @staticmethod
    def drain(st: Tab) -> str:
        notes, st.notes = st.notes, []
        return ("\n" + "\n".join(notes)) if notes else ""

    @staticmethod
    def window(text: str, offset: int, limit: int) -> str:
        out = text[offset:offset + limit]
        left = len(text) - offset - limit
        if left > 0:
            out += f"\n\n[{left} more characters: --offset {offset + limit}]"
        return out

    # --- commands ----------------------------------------------------------------

    async def cmd_ping(self, req):
        return {"pid": os.getpid(), "since": self.started, "out": "ok"}

    async def cmd_open(self, req):
        from . import browse, browse_agent, browse_tabs as tabs, browser_workspaces as bw
        url, session = self.clean_url(req["url"]), req["session"]
        if req.get("clean") or req.get("device"):
            ctx = await self.clean_context(session, req.get("device"))
            page = await ctx.new_page()
            target = await browse.atarget(ctx, page, self.clean_seen)
            workspace = "clean"
        else:
            workspace = bw.name(req.get("workspace"))

            def admit():
                bw.assert_agent(workspace, url if url.startswith("http") else None)
                bw.admit(workspace)
            try:
                await asyncio.to_thread(admit)
            except bw.Busy as e:
                raise Refused(str(e)) from None
            mine = tabs.rows(session=session, workspace=workspace)
            limit = int(config.get("browser.session_tabs", 10))
            if len(mine) >= limit:
                raise Refused(f"this session already has {len(mine)} tabs open (limit {limit}). Close the ones "
                              "you are done with: `herald browse close <tab>` or `herald browse close --all`.\n"
                              + "\n".join(f"  {r['tab']}  {(r['url'] or '')[:100]}" for r in mine))
            c = await self.conn(workspace)
            if len(c.ctx.pages) >= int(config.get("browser.max_tabs", 40)):
                raise Refused("this browser is at its tab limit; `herald browse tabs --all` shows whose they are")
            page, target = await browse.abackground_page(c.ctx, c.cdp, c.seen)
        tab = tabs.claim(workspace, target, session, url=url)
        st = self.track(workspace, tab, target, page, session)
        if req.get("keep"):
            tabs.keep(workspace, tab, float(req["keep"]))
        async with st.lock:
            if req.get("viewport"):
                w, h = (int(n) for n in str(req["viewport"]).lower().split("x"))
                await page.set_viewport_size({"width": w, "height": h})
            await self.goto(page, url, int(req.get("wait") or 4000))
            if st.jobs:
                await asyncio.wait(st.jobs, timeout=20)
            out = await self.head(st) + self.drain(st)
            if req.get("snap"):
                out += f"\n{browse.UNTRUSTED}\n" + await browse_agent.snapshot(page)
            elif req.get("text"):
                out += f"\n{browse.UNTRUSTED}\n" + self.window(await self.visible_text(page), 0, TEXT_LIMIT)
        tabs.touch(workspace, tab, page.url)
        return {"out": out, "tab": tab}

    async def cmd_fetch(self, req):
        """One public page in a context of its own, thrown away afterwards."""
        from . import browse
        url = self.clean_url(req["url"])
        browser = await self.clean_browser()
        ctx = await browser.new_context(user_agent=browse.DESKTOP_UA, viewport={"width": 1280, "height": 900})
        try:
            page = await ctx.new_page()
            await self.goto(page, url, int(req.get("wait") or 5000))
            out = f"{page.url}\n{browse.UNTRUSTED}\n"
            if req.get("html"):
                out += (await page.content())[:int(req.get("max") or 30000)]
            else:
                out += (await self.visible_text(page, req.get("selector") or "body"))[:int(req.get("max") or 20000)]
            if req.get("links"):
                seen = dict.fromkeys(f"{label[:80]}\t{href}" for label, href in await page.evaluate(browse.LINKS_JS))
                out += "\n\n[links]\n" + "\n".join(seen)
            return {"out": out}
        finally:
            with contextlib.suppress(Exception):
                await ctx.close()

    async def cmd_tabs(self, req):
        from . import browse, browse_tabs as tabs
        everyone = req.get("all")
        rows = tabs.rows() if everyone else tabs.rows(session=req["session"])
        if req.get("workspace"):
            rows = [r for r in rows if r["workspace"] == req["workspace"]]
        if not rows:
            return {"out": "no tabs open" + ("" if everyone else
                    f" in session {req['session']}; `herald browse tabs --all` lists everyone's")}
        now, lines = time.time(), []
        protected = {}
        for r in rows:
            if r["workspace"] not in protected and r["workspace"] != "clean":
                protected[r["workspace"]] = await asyncio.to_thread(browse.protected_tabs, r["workspace"])
            st = self.tabs.get((r["workspace"], r["tab"]))
            title = ""
            if st is not None and not st.page.is_closed():
                with contextlib.suppress(Exception):
                    title = (await asyncio.wait_for(st.page.title(), 2))[:40]
            flags = []
            if tabs.with_person(r):
                flags.append("with the user")
            if r["tab"] in protected.get(r["workspace"], ()):
                flags.append("application")
            if (r["keep_until"] or 0) > now:
                flags.append("kept")
            owner = r["session"] or ("nobody" if not r["pid"] else "")
            if r["pid"]:
                owner = f"{owner or 'process'} (pid {r['pid']})"
            idle = int((now - r["used"]) / 60)
            where = "" if r["workspace"] == "default" else f"  [{r['workspace']}]"
            lines.append(f"  {r['tab']}{where}  {title:<40}  {(r['url'] or '')[:100]}"
                         + (f"  owner {owner}" if everyone else "")
                         + (f"  idle {idle}m" if idle >= 5 else "")
                         + (f"  ({', '.join(flags)})" if flags else ""))
        return {"out": "\n".join(lines)}

    async def cmd_close(self, req):
        from . import browse, browse_tabs as tabs
        session, out = req["session"], []
        if req.get("all"):
            rows = tabs.rows(session=session)
        else:
            rows = []
            for ref in req.get("tabs") or []:
                st = await self.resolve(dict(req, tab=ref))
                rows.append(tabs.get(st.tab, st.workspace))
        for r in rows:
            if tabs.with_person(r):
                out.append(f"{r['tab']} is with the user; left open")
                continue
            if r["workspace"] != "clean" and req.get("all") \
                    and r["tab"] in await asyncio.to_thread(browse.protected_tabs, r["workspace"]):
                out.append(f"{r['tab']} holds an application draft; left open")
                continue
            page = (await self.pages(r["workspace"])).get(r["tab"])
            if page is not None and not page.is_closed():
                if r["workspace"] != "clean" and len((await self.conn(r["workspace"])).ctx.pages) <= 1:
                    await page.goto("about:blank")          # Chromium exits with its last tab
                else:
                    await page.close()
            tabs.forget(r["workspace"], r["tab"])
            self.tabs.pop((r["workspace"], r["tab"]), None)
            out.append(f"closed {r['tab']}")
        await self.drop_empty_contexts()
        return {"out": "\n".join(out) or "nothing to close"}

    async def drop_empty_contexts(self, session: str | None = None) -> None:
        for key, ctx in list(self.contexts.items()):
            if not ctx.pages and (session is None or key[0] == session):
                self.contexts.pop(key, None)
                with contextlib.suppress(Exception):
                    await ctx.close()

    async def cmd_go(self, req):
        async with self.using(req, mutates=True) as st:
            op = req.get("op") or "go"
            if op == "go":
                await self.goto(st.page, self.clean_url(req["url"]), int(req.get("wait") or 4000))
            else:
                try:
                    await {"back": st.page.go_back, "forward": st.page.go_forward,
                           "reload": st.page.reload}[op](timeout=45000)
                except Exception as e:                        # noqa: BLE001
                    raise Refused(f"{op} failed: {str(e).splitlines()[0][:200]}") from None
                await self.settle(st.page, int(req.get("wait") or 3000))
            return {"out": await self.head(st) + self.drain(st)}

    async def cmd_text(self, req):
        from . import browse
        async with self.using(req) as st:
            text = await self.visible_text(st.page, req.get("selector") or "body")
            return {"out": f"{await self.head(st)}\n{browse.UNTRUSTED}\n"
                    + self.window(text, int(req.get("offset") or 0), int(req.get("max") or TEXT_LIMIT))
                    + self.drain(st)}

    async def cmd_links(self, req):
        from . import browse
        async with self.using(req) as st:
            rx = re.compile(req["match"], re.I) if req.get("match") else None
            seen, lines = set(), []
            for fr in st.page.frames:
                try:
                    pairs = await fr.evaluate(browse.LINKS_JS)
                except Exception:                             # noqa: BLE001
                    continue
                for label, href in pairs:
                    if href in seen or (rx and not (rx.search(label) or rx.search(href))):
                        continue
                    seen.add(href)
                    lines.append(f"{label[:80]}\t{href}")
            return {"out": f"{browse.UNTRUSTED}\n" + ("\n".join(lines) or "no links")}

    async def cmd_html(self, req):
        from . import browse
        async with self.using(req) as st:
            sel = req.get("selector") or "html"
            try:
                h = await st.page.content() if sel == "html" else await st.page.locator(sel).first.inner_html(timeout=5000)
            except Exception as e:                            # noqa: BLE001
                raise Refused(f"nothing matches {sel!r}: {str(e).splitlines()[0][:160]}") from None
            return {"out": f"{browse.UNTRUSTED}\n" + self.window(h, int(req.get("offset") or 0),
                                                                  int(req.get("max") or 30000))}

    async def cmd_snap(self, req):
        from . import browse, browse_agent
        async with self.using(req) as st:
            try:
                body = await browse_agent.snapshot(
                    st.page, scope=req.get("scope"), full=bool(req.get("full")), whole=bool(req.get("page")),
                    find=req.get("find"), controls=bool(req.get("controls")))
            except Exception as e:                            # noqa: BLE001
                raise Refused(f"could not read the page: {str(e).splitlines()[0][:200]}") from None
            return {"out": f"{await self.head(st)}\n{browse.UNTRUSTED}\n{body}{self.drain(st)}"}

    async def cmd_do(self, req):
        from . import browse, browse_agent
        steps = browse_agent.parse(req.get("steps") or [])
        if not steps:
            raise Refused("give steps: " + browse_agent.STEP_HELP)
        approval = None
        if req.get("approval"):
            approval = await asyncio.to_thread(browse_agent.approval_for, int(req["approval"]), req.get("covers") or "")
        ends_with_snap = not req.get("quiet") and not any(v == "snap" for v, *_ in steps)
        if ends_with_snap:
            steps.append(["snap"])
        async with self.using(req, mutates=True) as st:
            try:
                log = await browse_agent.run_steps(st.page, steps, approval=approval, state=st)
                ok = True
            except browse_agent.StepError as e:
                log, ok = e.log, False
                with contextlib.suppress(Exception):
                    log.append(await browse_agent.snapshot(st.page))
            if approval and approval.get("used"):
                await asyncio.to_thread(browse_agent.record_approved_click, approval, st.page.url)
            if st.jobs:
                await asyncio.wait(st.jobs, timeout=20)
            notes = self.drain(st).strip()
            # What the page did (a dialog, a download, a new tab) reads best
            # after the steps and before the closing snapshot.
            cut = len(log) - 1 if ends_with_snap and ok and log else len(log)
            body = "\n".join(log[:cut] + ([notes] if notes else []) + log[cut:])
            return {"ok": ok, "out": f"{await self.head(st)}\n{browse.UNTRUSTED}\n{body}"}

    async def cmd_click(self, req):
        """Click by visible text, for when no snapshot has been taken."""
        from . import browse_agent
        async with self.using(req, mutates=True) as st:
            page, target = st.page, req["target"]
            if target.startswith(("css=", "#", ".", "[", "xpath=")):
                loc = page.locator(target.removeprefix("css=")).first
            else:
                loc = page.get_by_role("button", name=target).or_(
                    page.get_by_role("link", name=target)).or_(page.get_by_text(target, exact=False)).first
            try:
                label = await browse_agent._label(loc) or target
                await browse_agent.guard_click(page, label)
                await loc.click(timeout=10000)
            except SystemExit as e:
                raise Refused(str(e)) from None
            except Exception as e:                            # noqa: BLE001
                raise Refused(f"could not click {target!r}: {str(e).splitlines()[0][:200]}. "
                              "`snap` shows what is on the page.") from None
            await browse_agent.quiet(page, int(req.get("wait") or 2500))
            return {"out": f"clicked {label.strip()[:60]!r}\n{await self.head(st)}{self.drain(st)}"}

    async def cmd_press(self, req):
        from . import browse_agent
        async with self.using(req, mutates=True) as st:
            try:
                if req["key"].lower() == "enter":
                    await browse_agent.guard_enter(st.page)
                await st.page.keyboard.press(req["key"])
            except SystemExit as e:
                raise Refused(str(e)) from None
            await browse_agent.quiet(st.page, int(req.get("wait") or 2000))
            return {"out": f"pressed {req['key']}\n{await self.head(st)}{self.drain(st)}"}

    async def cmd_scroll(self, req):
        async with self.using(req) as st:
            await st.page.mouse.wheel(0, int(req.get("dy") or 900))
            await st.page.wait_for_timeout(500)
            return {"out": "scrolled"}

    async def cmd_shot(self, req):
        from . import browse
        async with self.using(req) as st:
            folder = browse.home() / "shots"
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            out = req.get("path") or str(folder / f"{st.tab}-{int(time.time())}.png")
            try:
                if req.get("viewport"):
                    w, h = (int(n) for n in str(req["viewport"]).lower().split("x"))
                    await st.page.set_viewport_size({"width": w, "height": h})
                    await st.page.wait_for_timeout(300)
                if req.get("ref"):
                    await st.page.locator(f"aria-ref={req['ref']}").screenshot(path=out, timeout=15000)
                else:
                    await st.page.screenshot(path=out, full_page=bool(req.get("full")), timeout=20000)
            except Exception as e:                            # noqa: BLE001
                raise Refused(f"screenshot failed: {str(e).splitlines()[0][:200]}") from None
            return {"out": out}

    async def cmd_pdf(self, req):
        from . import browse
        async with self.using(req) as st:
            folder = browse.home() / "shots"
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            out = req.get("path") or str(folder / f"{st.tab}-{int(time.time())}.pdf")
            cdp = await st.page.context.new_cdp_session(st.page)
            try:
                data = (await cdp.send("Page.printToPDF", {"printBackground": True}))["data"]
            except Exception as e:                            # noqa: BLE001
                raise Refused(f"could not print this page: {str(e).splitlines()[0][:200]}") from None
            finally:
                with contextlib.suppress(Exception):
                    await cdp.detach()
            pathlib.Path(out).write_bytes(base64.b64decode(data))
            return {"out": out}

    async def cmd_eval(self, req):
        from . import browse
        js = req["js"]
        if ACTS.search(js):
            raise Refused("refused: `eval` is for reading a page. This script clicks, submits or sends, which "
                          "would go around the check on what a click submits. Act with `herald browse do`.")
        async with self.using(req) as st:
            try:
                if req.get("ref"):
                    value = await st.page.locator(f"aria-ref={req['ref']}").evaluate(js, timeout=15000)
                else:
                    value = await st.page.evaluate(js)
            except Exception as e:                            # noqa: BLE001
                raise Refused(f"script failed: {str(e).splitlines()[0][:300]}") from None
            text = value if isinstance(value, str) else json.dumps(value, indent=1, ensure_ascii=False, default=str)
            return {"out": f"{browse.UNTRUSTED}\n" + self.window(text or "", 0, int(req.get("max") or TEXT_LIMIT))
                    + self.drain(st)}

    async def cmd_console(self, req):
        from . import browse
        async with self.using(req) as st:
            rows = list(st.console)
            if req.get("errors"):
                rows = [r for r in rows if r[1] in ("error", "pageerror", "warning")]
            if req.get("clear"):
                st.console.clear()
            if not rows:
                return {"out": "nothing logged since this tab was opened by the daemon"}
            return {"out": f"{browse.UNTRUSTED}\n" + "\n".join(
                f"{time.strftime('%H:%M:%S', time.localtime(ts))} {kind:<9} {text}" for ts, kind, text in rows[-80:])}

    async def cmd_network(self, req):
        from . import browse
        async with self.using(req) as st:
            if req.get("body"):
                entry = next((r for r in st.requests if r["n"] == int(req["body"])), None)
                if entry is None or entry.get("response") is None:
                    raise Refused(f"no stored response {req['body']}; `herald browse network {st.tab}` lists them")
                try:
                    body = await entry["response"].text()
                except Exception as e:                        # noqa: BLE001
                    raise Refused(f"the browser no longer has that body: {str(e).splitlines()[0][:160]}") from None
                return {"out": f"{entry['method']} {entry['status']} {entry['url']}\n{browse.UNTRUSTED}\n"
                        + self.window(body, int(req.get("offset") or 0), int(req.get("max") or TEXT_LIMIT))}
            rows = list(st.requests)
            if not req.get("all"):
                rows = [r for r in rows if r["type"] in ("xhr", "fetch", "document", "websocket", "eventsource")
                        or (r["status"] or 0) >= 400 or r["status"] is None]
            if req.get("match"):
                rx = re.compile(req["match"], re.I)
                rows = [r for r in rows if rx.search(r["url"])]
            if req.get("clear"):
                st.requests.clear()
            if not rows:
                return {"out": "no matching requests since this tab was opened by the daemon"}
            return {"out": f"{browse.UNTRUSTED}\n" + "\n".join(
                f"{r['n']:>4}  {r['method']:<6} {r['status'] or 'FAILED':<6} {r['type']:<9} {r['url'][:160]}"
                + (f"  ({r['failure']})" if r.get("failure") else "") for r in rows[-80:])
                + f"\n\n`herald browse network {st.tab} --body N` prints a response body."}

    async def cmd_keep(self, req):
        from . import browse_tabs as tabs
        st = await self.resolve(req)
        hours = float(req.get("hours") or 24)
        tabs.keep(st.workspace, st.tab, hours)
        return {"out": f"tab {st.tab} stays open for {hours:g} hours even if idle" if hours > 0
                else f"tab {st.tab} follows the usual idle rule again"}

    async def cmd_locate(self, req):
        """Where a tab is, without holding it. For `login`, which the client
        finishes because only it knows which Telegram topic asked."""
        from . import browse_tabs as tabs
        st = await self.resolve(req)
        row = tabs.get(st.tab, st.workspace) or {}
        return {"out": "", "tab": st.tab, "workspace": st.workspace, "url": st.page.url,
                "with_person": tabs.with_person(row)}

    async def cmd_returned(self, req):
        from . import browse_tabs as tabs, browser_workspaces as bw
        st = await self.resolve(req)
        deadline = time.monotonic() + float(req.get("wait") or 0)
        while True:
            row = tabs.get(st.tab, st.workspace)
            ws = bw.get(st.workspace) if st.workspace != "clean" else None
            if not tabs.with_person(row) and (ws is None or ws["state"] == "agent"):
                return {"out": f"returned: tab {st.tab} is yours again\n{await self.head(st)}"}
            if time.monotonic() >= deadline:
                return {"ok": False, "out": f"tab {st.tab} is still with the user"}
            await asyncio.sleep(1)

    async def cmd_human(self, req):
        """What the person's view page asks for. Not offered by the CLI."""
        page = (await self.pages(req.get("workspace") or "default")).get(str(req.get("tab", "")).lower())
        if page is None or page.is_closed():
            raise Refused("the tab is gone")
        op = req.get("op")
        if op == "front":
            await page.bring_to_front()
        elif op == "frame":
            await page.bring_to_front()
            shot = await page.screenshot(type="jpeg", quality=70, timeout=15000)
            return {"out": "", "jpeg": base64.b64encode(shot).decode()}
        elif op == "bounds":
            cdp = await page.context.new_cdp_session(page)
            try:
                win = (await cdp.send("Browser.getWindowForTarget"))["windowId"]
                await cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": {
                    "left": 0, "top": 0, "width": int(req["w"]), "height": int(req["h"]), "windowState": "normal"}})
            finally:
                with contextlib.suppress(Exception):
                    await cdp.detach()
            await page.bring_to_front()
        elif op == "click":
            await page.mouse.click(float(req["x"]), float(req["y"]))
        elif op == "type":
            await page.keyboard.type(str(req.get("text", ""))[:2000])
        elif op == "key" and req.get("key") in ("Enter", "Tab", "Backspace", "Escape"):
            await page.keyboard.press(req["key"])
        elif op == "scroll":
            await page.mouse.wheel(0, max(-3000, min(3000, int(req.get("dy", 0)))))
        elif op == "back":
            await page.go_back(timeout=15000)
        else:
            raise Refused("unknown action")
        return {"out": "ok"}

    # --- health ------------------------------------------------------------------

    def workspaces(self) -> list[str]:
        from . import browser_remote, browser_workspaces as bw
        names = ["default"]
        try:
            names += [w["id"] for w in bw.all_workspaces()
                      if w["id"] != "default" and w["state"] != "closed" and not browser_remote.is_remote(w["id"])]
        except Exception:                                     # noqa: BLE001
            pass
        return names

    async def cmd_status(self, req):
        from . import browse, browse_tabs as tabs
        lines = [f"daemon    up {int((time.time() - self.started) / 60)} min, pid {os.getpid()}, "
                 f"{self.active - 1} other command(s) running"]
        for ws in await asyncio.to_thread(self.workspaces):
            if not await asyncio.to_thread(browse.alive, ws):
                if ws == "default":
                    lines.append("default   not running; the next command starts it")
                continue
            report = await asyncio.to_thread(browse.health, ws)
            if not report["ok"]:
                lines.append(f"{ws:<9} NOT WORKING: it is running but does not answer ({report['error']}). "
                             f"`herald browse heal --workspace {ws} --restart`")
                continue
            bad = [p for p in report["pages"] if p["state"] != "ok"]
            tabs.sync(ws, {p["id"]: p["url"] for p in report["pages"]})
            owners = collections.Counter(r["session"] or "nobody" for r in tabs.rows(workspace=ws))
            lines.append(f"{ws:<9} working, {len(report['pages'])} tabs"
                         + (f" ({', '.join(f'{n} {s}' for s, n in owners.most_common(6))})" if owners else "")
                         + (f"; {len(bad)} not answering: {', '.join(p['tab'] for p in bad)} (`herald browse heal`)"
                            if bad else ""))
        sessions = collections.Counter(t.session for k, t in self.tabs.items() if k[0] == "clean")
        lines.append("clean     " + (f"{sum(sessions.values())} tabs ({', '.join(f'{n} {s}' for s, n in sessions.items())})"
                                     if sessions else "idle")
                     + f"; WebGL: {await self.clean_webgl()}")
        lines.append(f"view      {browse.view_base() or 'off: set surfaces.browser_view.url'}")
        return {"out": "\n".join(lines)}

    async def cmd_heal(self, req):
        from . import browse, browser_workspaces as bw
        ws = bw.name(req.get("workspace"))
        out = []
        if not await asyncio.to_thread(browse.alive, ws):
            return {"out": f"the {ws} browser is not running; the next command starts it"}
        report = await asyncio.to_thread(browse.heal, ws, force=True)
        for c in report["closed"]:
            out.append(f"closed {c['tab']} ({c['why']}): {c['url'][:90]}")
        for c in report["stuck"]:
            out.append(f"could not close {c['tab']} ({c['why']}): {c['url'][:90]}")
        if req.get("restart") or not report["ok"]:
            why = await asyncio.to_thread(browse.may_restart, ws)
            if not req.get("restart"):
                out.append(f"the browser does not answer ({report['error']}); `--restart` starts a fresh one")
            elif why and not req.get("force"):
                out.append(f"not restarted: {why}. Its open tabs would be lost. `--restart --force` does it anyway.")
            else:
                await self.forget_conn(ws)
                ok = await asyncio.to_thread(browse.restart, ws)
                out.append("restarted; sign-ins are kept, open tabs are gone" if ok
                           else "the browser did not come back; see " + str(browse.home() / "chrome.log"))
        return {"out": "\n".join(out) or f"the {ws} browser is working; nothing needed closing "
                                          f"({len(report['pages'])} tabs)",
                "ok": report["ok"] or bool(req.get("restart"))}

    async def forget_conn(self, ws: str) -> None:
        c = self.conns.pop(ws, None)
        if c is not None:
            with contextlib.suppress(Exception):
                await c.browser.close()

    async def janitor(self) -> None:
        from . import browse, browse_tabs as tabs
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), JANITOR_SECONDS)
                return
            except asyncio.TimeoutError:
                pass
            try:
                for ws in await asyncio.to_thread(self.workspaces):
                    if not await asyncio.to_thread(browse.alive, ws):
                        self.failures.pop(ws, None)
                        continue
                    report = await asyncio.to_thread(browse.heal, ws)
                    self.health[ws] = {"ok": report["ok"], "error": report["error"], "at": time.time(),
                                       "tabs": len(report["pages"])}
                    if report["ok"]:
                        self.failures.pop(ws, None)
                        continue
                    self.failures[ws] += 1
                    # Three minutes of a running browser that will not talk.
                    if self.failures[ws] >= 3 and not await asyncio.to_thread(browse.may_restart, ws):
                        await self.forget_conn(ws)
                        await asyncio.to_thread(browse.restart, ws)
                        self.failures.pop(ws, None)
                        with open(browse.home() / "janitor.log", "a") as f:
                            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {ws} restarted: it stopped answering\n")
                # Clean tabs follow the same idle rule; their browser goes
                # when the last one does.
                for r in tabs.expired("clean"):
                    st = self.tabs.pop(("clean", r["tab"]), None)
                    if st is not None:
                        with contextlib.suppress(Exception):
                            await st.page.close()
                    tabs.forget("clean", r["tab"])
                live = {t.tab for k, t in self.tabs.items() if k[0] == "clean"}
                for r in tabs.rows(workspace="clean"):
                    if r["tab"] not in live:
                        tabs.forget("clean", r["tab"])
                await self.drop_empty_contexts()
                if self.clean is not None and not self.contexts and self.active == 0 \
                        and time.time() - self.last_request > 300:
                    with contextlib.suppress(Exception):
                        await self.clean.close()
                    self.clean = None
                (config.HOME / "run" / "browse-health.json").write_text(json.dumps(self.health))
                for old in (browse.home() / "shots").glob("*") if (browse.home() / "shots").exists() else ():
                    if time.time() - old.stat().st_mtime > 2 * 86400:
                        old.unlink(missing_ok=True)
                if not os.environ.get("INVOCATION_ID") and self.active == 0 and not self.contexts \
                        and time.time() - self.last_request > IDLE_EXIT:
                    self.stop.set()
            except Exception as e:                            # noqa: BLE001
                print(f"janitor: {type(e).__name__}: {str(e)[:300]}", file=sys.stderr, flush=True)

    # --- the socket ----------------------------------------------------------------

    async def handle(self, req: dict) -> dict:
        from . import browse_tabs as tabs, browser_workspaces as bw
        cmd = str(req.get("cmd") or "")
        fn = getattr(self, "cmd_" + cmd, None)
        if fn is None:
            return {"ok": False, "out": f"unknown command {cmd!r}"}
        req["session"] = tabs.session_name(req.get("session"))
        self.active += 1
        self.last_request = time.time()
        try:
            if req.get("workspace"):
                req["workspace"] = "clean" if req["workspace"] == "clean" else bw.name(req["workspace"])
            resp = await asyncio.wait_for(fn(req), float(req.get("timeout") or 180))
            resp.setdefault("ok", True)
            return resp
        except Refused as e:
            return {"ok": False, "out": str(e)}
        except SystemExit as e:
            return {"ok": False, "out": str(e)}
        except asyncio.TimeoutError:
            return {"ok": False, "out": f"`{cmd}` did not finish in {float(req.get('timeout') or 180):.0f} "
                                        "seconds and was stopped. `herald browse status` says whether the browser works."}
        except Exception as e:                                # noqa: BLE001
            first = (str(e).splitlines() or [""])[0][:400]
            return {"ok": False, "out": f"{type(e).__name__}: {first}"}
        finally:
            self.active -= 1
            self.last_request = time.time()

    async def client(self, reader, writer) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                resp = await self.handle(json.loads(line))
            except ValueError:
                resp = {"ok": False, "out": "bad request"}
            writer.write(json.dumps(resp).encode() + b"\n")
            await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def cmd_shutdown(self, req):
        self.stop.set()
        return {"out": "stopping"}

    async def serve(self) -> int:
        from playwright.async_api import async_playwright
        path = sock_path()
        # One daemon: whoever holds this lock is it.
        guard = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another browse daemon is already running", file=sys.stderr)
            return 0
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        from . import browse_tabs as tabs
        for row in tabs.rows(workspace="clean"):
            tabs.forget("clean", row["tab"])       # the clean browser died with the last daemon
        async with async_playwright() as pw:
            self.pw = pw
            server = await asyncio.start_unix_server(self.client, path=path, limit=8 * 1024 * 1024)
            os.chmod(path, 0o600)
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self.stop.set)
            janitor = asyncio.ensure_future(self.janitor())
            print(f"herald browse daemon: listening on {path}", flush=True)
            await self.stop.wait()
            server.close()
            janitor.cancel()
            with contextlib.suppress(Exception):
                os.unlink(path)
            for c in list(self.conns.values()):
                with contextlib.suppress(Exception):
                    await c.browser.close()            # disconnects; the browser stays
            if self.clean is not None:
                with contextlib.suppress(Exception):
                    await self.clean.close()
        return 0


# --- the client -----------------------------------------------------------------

def _connect(timeout: float):
    import socket
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(timeout)
    try:
        c.connect(sock_path())
        return c
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        c.close()
        return None


def start() -> None:
    """Start the daemon: through its unit when there is one, so it is not a
    child of whichever turn or collector happened to need it first."""
    import shutil
    import subprocess
    unit = "herald-browse-daemon.service"
    if shutil.which("systemctl") and subprocess.run(
            ["systemctl", "--user", "cat", unit], capture_output=True, timeout=10).returncode == 0:
        if subprocess.run(["systemctl", "--user", "start", unit], capture_output=True, timeout=30).returncode == 0:
            return
    log = config.HOME / "browser" / "daemon.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(config.ROOT / "lib") + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("INVOCATION_ID", None)
    with open(log, "a") as out:
        subprocess.Popen([sys.executable, "-m", "herald.browse_daemon"], stdout=out, stderr=out,
                         stdin=subprocess.DEVNULL, start_new_session=True, cwd=str(config.ROOT), env=env)


def request(req: dict, *, timeout: float = 200, autostart: bool = True) -> dict:
    """Send one request, starting the daemon if none is running."""
    c = _connect(timeout)
    if c is None:
        if not autostart:
            raise ConnectionError("the browse daemon is not running")
        start()
        deadline = time.monotonic() + 40
        while c is None and time.monotonic() < deadline:
            time.sleep(0.2)
            c = _connect(timeout)
        if c is None:
            raise SystemExit("the browse daemon did not start; see "
                             f"{config.HOME / 'browser' / 'daemon.log'} or "
                             "`journalctl --user -u herald-browse-daemon`")
    with c:
        c.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = c.recv(1 << 20)
            if not chunk:
                break
            buf += chunk
    if not buf:
        raise SystemExit("the browse daemon closed the connection without answering; "
                         "`herald browse status` shows whether it is running")
    return json.loads(buf)


def running() -> bool:
    c = _connect(2)
    if c is None:
        return False
    c.close()
    return True


def main() -> int:
    return asyncio.run(Daemon().serve())


if __name__ == "__main__":
    sys.exit(main())
