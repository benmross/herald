"""The setup wizard in a browser.

The same steps as the terminal, rendered as a page. It exists because the
terminal version asks a non-technical person to paste a JSON file into a shell
prompt and to write a thousand words in `nano`, and neither of those is a thing
they will do.

Deliberately small: `http.server` from the standard library, one HTML file, no
build step and no dependencies. Herald already refuses to add a package it does
not need, and a setup wizard that cannot run until you have installed its
dependencies is a joke at the user's expense.

Three things about it are load-bearing rather than incidental:

**It binds to 127.0.0.1 only.** This page can read the machine's Google
credentials and write its config; it has no business being reachable from the
network. Someone setting Herald up on a server gets an SSH port-forward
instruction rather than a bind address.

**Every request carries a token** printed with the URL. The bind already keeps
other machines out; the token keeps *other users on this machine* out, which is
the case a bind address does not cover.

**Long steps run in a worker thread** and are polled. The Google consent flow
waits for a human to click through a sign-in on another device, and the
interview's write-up is a model pass that takes minutes -- neither can be an
HTTP request that either blocks a single-threaded server or times out.
"""

from __future__ import annotations

import json
import mimetypes
import pathlib
import secrets
import socket
import sys
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "lib"))

from herald import config  # noqa: E402
from setup import engine  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(18)

#: job id -> {"state": running|done, "outcome": {...}, "url": str|None}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def _job(job_id: str) -> dict:
    with JOBS_LOCK:
        return dict(JOBS.get(job_id) or {})


def _set_job(job_id: str, **fields) -> None:
    with JOBS_LOCK:
        JOBS.setdefault(job_id, {}).update(fields)


def _run_step(job_id: str, step_key: str, answers: dict) -> None:
    state = engine.State()
    step = engine.by_key(step_key)
    if step is None:
        _set_job(job_id, state="done",
                 outcome={"ok": False, "message": f"no such step: {step_key}"})
        return
    if step_key == "google":
        answers = dict(answers)
        answers["_on_url"] = lambda url: _set_job(job_id, url=url)
    try:
        outcome = step.apply(state, answers)
        _set_job(job_id, state="done", outcome={
            "ok": outcome.ok, "message": outcome.message, "detail": outcome.detail,
            "warnings": list(outcome.warnings), "more": outcome.more})
    except Exception as exc:                                        # noqa: BLE001
        _set_job(job_id, state="done", outcome={
            "ok": False, "message": f"{type(exc).__name__}: {exc}",
            "detail": traceback.format_exc()[-1500:], "warnings": [], "more": False})


def _overview_payload(step_key: str | None = None) -> dict:
    state = engine.State()
    rows = engine.overview(state)
    step = engine.by_key(step_key) if step_key else engine.next_step(state)
    if step is None:
        step = engine.by_key("done")
    prompt = engine.as_dict(step.prompt(state))
    status, detail = step.status(state)
    return {"steps": rows, "current": step.key, "title": step.title,
            "status": status, "detail": detail, "prompt": prompt,
            "agent": config.get("agent.name", "Herald")}


class Handler(BaseHTTPRequestHandler):
    server_version = "herald-setup"

    # ---- plumbing

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload, default=str).encode(),
                   "application/json; charset=utf-8")

    def _authorised(self, query: dict) -> bool:
        return (query.get("token", [None])[0] == TOKEN
                or self.headers.get("X-Herald-Token") == TOKEN)

    def log_message(self, *args):                                   # noqa: A002
        pass

    # ---- routes

    def do_GET(self):                                               # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path

        if path in ("/", "/index.html"):
            if not self._authorised(query):
                self._send(403, b"Open the full link printed in your terminal, "
                                b"including the part after token=.",
                           "text/plain; charset=utf-8")
                return
            self._send(200, (HERE / "app.html").read_bytes(),
                       "text/html; charset=utf-8")
            return

        if not self._authorised(query):
            self._json({"error": "unauthorised"}, 403)
            return

        if path == "/api/state":
            self._json(_overview_payload(query.get("step", [None])[0]))
            return

        if path.startswith("/api/job/"):
            self._json(_job(path.rsplit("/", 1)[-1]))
            return

        self._json({"error": "not found"}, 404)

    def do_POST(self):                                              # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorised(query):
            self._json({"error": "unauthorised"}, 403)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "bad json"}, 400)
            return

        if parsed.path == "/api/apply":
            job_id = uuid.uuid4().hex
            _set_job(job_id, state="running", url=None, outcome=None)
            threading.Thread(
                target=_run_step,
                args=(job_id, payload.get("step"), payload.get("answers") or {}),
                daemon=True).start()
            self._json({"job": job_id})
            return

        if parsed.path == "/api/draft":
            # The interview is an hour of typing; losing it to a closed tab is
            # not acceptable, so drafts are written as they are typed.
            text = payload.get("piece") or ""
            path = config.IDENTITY / "interview"
            path.mkdir(parents=True, exist_ok=True)
            (path / "draft.md").write_text(text)
            self._json({"saved": len(text.split())})
            return

        self._json({"error": "not found"}, 404)


def _forward_command(port: int) -> str:
    # Two ports, one command: the wizard's own, and the one Google redirects to
    # at the end of the sign-in. Forwarding only the first means the Google
    # step fails forty minutes in with "connection refused" in a browser tab.
    from setup.google import DEFAULT_PORT as google_port  # noqa: PLC0415
    host = socket.gethostname()
    return (f"ssh -N -L 127.0.0.1:{port}:127.0.0.1:{port} "
            f"-L 127.0.0.1:{google_port}:127.0.0.1:{google_port} $USER@{host}")


def serve(port: int = 8799) -> int:
    config.ensure_dirs()
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        print(f"cannot listen on 127.0.0.1:{port}: {exc}", file=sys.stderr)
        print("something else is using it -- try --port 8800", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{port}/?token={TOKEN}"
    print()
    print("  Open this in your browser:\n")
    print(f"      {url}\n")
    if engine.over_ssh():
        print("  You are connected over SSH, so first, on the computer you are")
        print("  sitting at, run this and leave it running until setup is done:\n")
        print(f"      {_forward_command(port)}\n")
    print("  The page can only be reached from this computer, and the code in")
    print("  the link keeps other people who use it out. Press Ctrl-C here when")
    print("  you have finished.\n", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped. `herald setup --web` starts it again.\n")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
