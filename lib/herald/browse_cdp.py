"""A minimal DevTools client, for asking a browser whether it actually works.

Why this is not Playwright: `connect_over_cdp` attaches to every tab in the
browser and waits for each to finish initialising before it returns. One tab
that never does makes every connection to that browser hang, and there are at
least two ordinary ways to get such a tab:

- a renderer that stopped answering. On 20 Nov 2026 three tabs left open for a
  week by finished sessions froze, and from then on every `herald browse`
  command and every run of a collector that reads through the browser timed
  out after three minutes (43 runs) while `/json/version` kept reporting the
  browser as up.
- a popup whose first navigation was blocked. It stays at an empty URL
  forever; the page answers, but Playwright never counts it as initialised
  (reproduced 21 Nov 2026 with `window.open` of a data: URL).

So the health check and the janitor cannot use the thing they are checking.
This speaks the protocol directly over a WebSocket, using only the standard
library: list the tabs, ask each one to evaluate `1`, and close the ones that
do not answer. It is deliberately tiny: text frames, one connection, no
events kept.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
import urllib.request
from urllib.parse import urlsplit


class CDPError(RuntimeError):
    pass


def http_json(port: int, path: str, timeout: float = 2.0):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return json.load(r)


def alive(port: int, timeout: float = 2.0) -> bool:
    """The DevTools HTTP endpoint answers. Says nothing about whether a
    connection would work; `probe` does that."""
    try:
        return bool(http_json(port, "/json/version", timeout))
    except Exception:
        return False


class Connection:
    """One WebSocket to the browser endpoint. Synchronous, with deadlines."""

    def __init__(self, port: int, timeout: float = 4.0):
        url = http_json(port, "/json/version", timeout)["webSocketDebuggerUrl"]
        u = urlsplit(url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        self.buf = b""
        head = self._until(b"\r\n\r\n", time.monotonic() + timeout)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise CDPError("the browser refused the DevTools connection")
        self.n = 0
        self.replies: dict[int, dict] = {}

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- frames ---------------------------------------------------------------

    def _fill(self, deadline: float) -> None:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        self.sock.settimeout(left)
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            raise TimeoutError from None
        if not chunk:
            raise CDPError("the browser closed the DevTools connection")
        self.buf += chunk

    def _until(self, mark: bytes, deadline: float) -> bytes:
        while mark not in self.buf:
            self._fill(deadline)
        head, self.buf = self.buf.split(mark, 1)
        return head

    def _take(self, n: int, deadline: float) -> bytes:
        while len(self.buf) < n:
            self._fill(deadline)
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.settimeout(5)
        self.sock.sendall(head + mask + body)

    def _read_message(self, deadline: float) -> dict | None:
        data = b""
        while True:
            b0, b1 = self._take(2, deadline)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._take(2, deadline))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._take(8, deadline))[0]
            payload = self._take(n, deadline)
            opcode = b0 & 0x0F
            if opcode == 0x9:                       # ping
                self._send_frame(0xA, payload)
                continue
            if opcode == 0x8:
                raise CDPError("the browser closed the DevTools connection")
            if opcode in (0x0, 0x1, 0x2):
                data += payload
                if b0 & 0x80:
                    try:
                        return json.loads(data)
                    except ValueError:
                        return None
            # pongs and anything else are skipped

    # -- protocol -------------------------------------------------------------

    def send(self, method: str, params: dict | None = None, session: str | None = None) -> int:
        self.n += 1
        msg = {"id": self.n, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self._send_frame(0x1, json.dumps(msg).encode())
        return self.n

    def collect(self, ids, deadline: float) -> dict[int, dict]:
        """Replies to `ids` that arrive before the deadline. Missing ones timed out."""
        want = set(ids)
        got = {i: self.replies.pop(i) for i in list(want) if i in self.replies}
        while want - got.keys():
            try:
                msg = self._read_message(deadline)
            except TimeoutError:
                break
            if msg and "id" in msg:
                if msg["id"] in want:
                    got[msg["id"]] = msg
                else:
                    self.replies[msg["id"]] = msg
        return got

    def call(self, method: str, params: dict | None = None, session: str | None = None,
             timeout: float = 4.0) -> dict:
        i = self.send(method, params, session)
        msg = self.collect([i], time.monotonic() + timeout).get(i)
        if msg is None:
            raise TimeoutError(f"{method} was not answered in {timeout:.0f}s")
        if "error" in msg:
            raise CDPError(f"{method}: {msg['error'].get('message')}")
        return msg.get("result", {})


def short(target_id: str) -> str:
    """The six-character tab id every `herald browse` command prints."""
    return target_id[:6].lower()


def probe(port: int, timeout: float = 3.0) -> dict:
    """Ask every tab to evaluate `1`.

    Returns {"ok", "browser", "error", "pages": [{"id", "tab", "url", "title",
    "opener", "state"}]} where state is "ok", "wedged" (attached but silent)
    or "stub" (an empty-URL page, which is a popup that never loaded if it is
    still empty on a second look).
    """
    out = {"ok": False, "browser": None, "error": None, "pages": []}
    try:
        with Connection(port, timeout) as c:
            out["browser"] = c.call("Browser.getVersion", timeout=timeout).get("product")
            infos = [t for t in c.call("Target.getTargets", timeout=timeout)["targetInfos"]
                     if t["type"] == "page" and not t["url"].startswith(("devtools://", "chrome-extension://"))]
            attach = {c.send("Target.attachToTarget", {"targetId": t["targetId"], "flatten": True}): t
                      for t in infos}
            attached = c.collect(attach, time.monotonic() + timeout)
            evals, sessions = {}, []
            for i, t in attach.items():
                sid = (attached.get(i) or {}).get("result", {}).get("sessionId")
                if sid:
                    sessions.append(sid)
                    evals[c.send("Runtime.evaluate", {"expression": "1", "returnByValue": True}, sid)] = t["targetId"]
            answered = c.collect(evals, time.monotonic() + timeout)
            fine = {evals[i] for i, m in answered.items() if "result" in m}
            for sid in sessions:
                c.send("Target.detachFromTarget", {"sessionId": sid})
            for t in infos:
                state = "ok" if t["targetId"] in fine else "wedged"
                if state == "ok" and not t["url"]:
                    state = "stub"
                out["pages"].append({"id": t["targetId"], "tab": short(t["targetId"]), "url": t["url"],
                                     "title": t.get("title", ""), "opener": t.get("openerId"),
                                     "state": state})
            out["ok"] = True
    except Exception as e:                                    # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return out


def close_targets(port: int, target_ids, timeout: float = 4.0) -> list[str]:
    """Close tabs by full target id. Returns the ids the browser no longer lists.

    A hung renderer cannot run its unload handlers, so after asking nicely
    this checks the list again; Chromium drops such a tab within a few
    seconds of closeTarget.
    """
    ids = list(target_ids)
    if not ids:
        return []
    try:
        with Connection(port, timeout) as c:
            sent = [c.send("Target.closeTarget", {"targetId": i}) for i in ids]
            c.collect(sent, time.monotonic() + timeout)
            deadline = time.monotonic() + 6
            while True:
                left = {t["targetId"] for t in c.call("Target.getTargets", timeout=timeout)["targetInfos"]}
                gone = [i for i in ids if i not in left]
                if len(gone) == len(ids) or time.monotonic() > deadline:
                    return gone
                time.sleep(0.3)
    except Exception:                                         # noqa: BLE001
        return []


def new_background_tab(port: int, url: str = "about:blank", timeout: float = 4.0) -> str:
    """Open a tab without raising it over whatever is in front. Returns its id."""
    with Connection(port, timeout) as c:
        return c.call("Target.createTarget", {"url": url, "background": True}, timeout=timeout)["targetId"]
