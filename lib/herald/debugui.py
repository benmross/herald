"""Read-only session observatory. No arbitrary paths, writes or model calls.

The listener is loopback-only. A configured Tailscale Serve URL may proxy it;
Host and Origin are checked to prevent rebinding and cross-origin reads.
"""
from __future__ import annotations

import datetime as dt
import contextlib
import functools
import json
import pathlib
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import config, transcripts

ASSETS = config.ROOT / "web" / "debug"
SID = re.compile(r"^[a-zA-Z0-9_-]{1,100}$")


def redactor():
    from .search import redactor as base_redactor
    from .leaks import SHAPES
    clean = base_redactor()
    sensitive = re.compile(r"^(?:authorization|cookie|set-cookie|password|passwd|access_token|refresh_token|client_secret|api_key|private_key)$", re.I)

    def scrub(value):
        if isinstance(value, dict):
            return {k: "[redacted]" if sensitive.fullmatch(k) else scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, str):
            value = clean(value)
            for shape, _ in SHAPES:
                value = shape.sub("[redacted]", value)
            return value
        return value
    return scrub


def connect():
    con = sqlite3.connect(config.DB_PATH.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def rows(sql, params=()):
    with contextlib.closing(connect()) as con:
        return [dict(r) for r in con.execute(sql, params)]


def records(path):
    try:
        with path.open(errors="replace") as source:
            for line in source:
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        yield obj
                except ValueError:
                    continue  # A live file may end halfway through a record.
    except (OSError, UnicodeError):
        return


def session_id(obj):
    p = obj.get("payload") or {}
    data = obj.get("data") or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    result = data.get("result") or {}
    params = data.get("params") or {}
    return (obj.get("session_id") or obj.get("sessionId") or
            (p.get("id") if obj.get("type") == "session_meta" else None) or
            data.get("session_id") or data.get("thread_id") or params.get("threadId") or
            (result.get("thread") or {}).get("id") or
            (data.get("payload") or {}).get("session_id") or data.get("resume"))


@functools.lru_cache(maxsize=512)
def capture_identity(path, size, modified):
    found = None
    for obj in records(pathlib.Path(path)):
        found = session_id(obj) or found
    return found


@functools.lru_cache(maxsize=1024)
def source_title(path):
    for i, obj in enumerate(records(pathlib.Path(path))):
        if i > 100:
            break
        p = obj.get("payload") or obj
        if not (obj.get("type") == "user" or p.get("role") == "user"):
            continue
        content = (obj.get("message") or {}).get("content") or p.get("content") or []
        text = content if isinstance(content, str) else "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") in ("text", "input_text"))
        text = transcripts.clean_user(text)
        if text:
            return " ".join(text.split())[:160]
    return None


class Catalog:
    def __init__(self):
        self.lock = threading.Lock()
        self.until = 0
        self.data = {}

    def get(self):
        with self.lock:
            if time.monotonic() < self.until:
                return self.data
            runs = rows("SELECT * FROM runs ORDER BY id")
            sessions = {}
            for r in runs:
                sid = r["session_id"] or f"run-{r['id']}"
                s = sessions.setdefault(sid, {"id": sid, "engine": r["engine"], "runs": [], "sources": []})
                s["runs"].append(r)
            paths = transcripts.locate()
            for path in (pathlib.Path.home() / ".codex" / "archived_sessions").glob("rollout-*.jsonl"):
                paths.setdefault(path.stem[-36:], path)
            for sid, path in paths.items():
                if sid not in sessions:
                    first = next(records(path), {})
                    cwd = (first.get("payload") or {}).get("cwd") or first.get("cwd")
                    # Claude paths are already scoped by transcripts.locate.
                    if path.name.startswith("rollout-") and cwd != str(config.ROOT):
                        continue
                engine = "codex" if path.name.startswith("rollout-") else "claude"
                s = sessions.setdefault(sid, {"id": sid, "engine": engine, "runs": [], "sources": []})
                s["sources"].append(("transcript", path))
                # Claude stores delegated work under the parent session.
                for child in sorted((path.parent / sid / "subagents").glob("*.jsonl")):
                    s["sources"].append(("subagent", child))
            for path in sorted((config.HOME / "debug-traces").glob("*.jsonl")):
                first = next(records(path), {})
                stat = path.stat()
                found = capture_identity(str(path), stat.st_size, stat.st_mtime_ns)
                sid = found or ("trace-" + path.stem)
                metadata = first.get("data") or {}
                s = sessions.setdefault(sid, {"id": sid, "engine": metadata.get("engine"), "runs": [], "sources": []})
                s["sources"].append(("capture", path))
            for s in sessions.values():
                rr = s["runs"]
                s.update(start=rr[0]["ts"] if rr else None,
                         updated=rr[-1]["ts"] if rr else None,
                         label=rr[-1]["label"] if rr else "terminal / in progress",
                         model=rr[-1]["model"] if rr else None,
                         turns=len(rr), errors=sum(bool(r["error"]) for r in rr),
                         duration_ms=sum(r["duration_ms"] or 0 for r in rr),
                         context_tokens=rr[-1]["context_tokens"] if rr else None,
                         bytes=sum(p.stat().st_size for _, p in s["sources"] if p.exists()))
                s["title"] = next((title for kind, path in s["sources"] if kind == "transcript" and (title := source_title(str(path)))), s["label"])
                if s["sources"]:
                    modified = max(p.stat().st_mtime for _, p in s["sources"] if p.exists())
                    s["updated"] = max(s["updated"] or "", dt.datetime.fromtimestamp(modified, dt.timezone.utc).isoformat())
            self.data, self.until = sessions, time.monotonic() + 8
            return sessions


def normalize(obj, seq):
    """Keep the entire record alongside a readable, provider-neutral summary."""
    raw = obj
    ts = obj.get("timestamp") or obj.get("ts")
    channel = obj.get("channel")
    if channel:
        data = obj.get("data")
        if isinstance(data, str):
            try:
                obj = json.loads(data)
            except ValueError:
                obj = {"text": data}
        elif isinstance(data, dict):
            obj = data
        else:
            obj = {}
    p = obj.get("payload") or obj
    method = obj.get("method") or ""
    params = obj.get("params") or {}
    item = params.get("item") or obj.get("item") or {}
    typ = p.get("type") or obj.get("type") or method or channel or "event"
    kind, name, text, call_id, context, window = "event", typ, "", None, None, None
    message = obj.get("message") or {}
    content = message.get("content") or p.get("content") or []
    if typ in ("user", "assistant", "message"):
        kind = p.get("role") or typ
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(c.get("text") or c.get("thinking") or "" for c in content if isinstance(c, dict))
            uses = [c for c in content if isinstance(c, dict) and c.get("type") in ("tool_use", "tool_result")]
            if uses:
                kind = "tool"
                name = ", ".join(c.get("name") or "result" for c in uses)
                text = json.dumps(uses, indent=2, ensure_ascii=False)
                call_id = uses[0].get("id") or uses[0].get("tool_use_id")
        usage = message.get("usage") or {}
        if usage.get("input_tokens") is not None:
            context = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    elif typ in ("function_call", "custom_tool_call", "function_call_output", "custom_tool_call_output"):
        kind, name = "tool", p.get("name") or typ
        text = p.get("arguments") or p.get("input") or p.get("output") or ""
        call_id = p.get("call_id")
    elif method.startswith("item/") or typ in ("item.started", "item.completed"):
        name = item.get("type") or method
        kind = "tool" if name in ("commandExecution", "command_execution", "mcpToolCall", "mcp_tool_call", "fileChange", "file_change", "webSearch") else "event"
        text = json.dumps(item, ensure_ascii=False, indent=2)
        call_id = item.get("id")
    elif typ == "event_msg":
        kind = "context" if p.get("type") == "token_count" else "event"
    if p.get("type") == "token_count":
        info = p.get("info") or {}
        usage = info.get("last_token_usage") or {}
        context, window = usage.get("input_tokens"), info.get("model_context_window")
        kind, name = "context", "token usage"
    if method == "thread/tokenUsage/updated":
        tokens = params.get("tokenUsage") or {}
        context, window = (tokens.get("last") or {}).get("inputTokens"), tokens.get("modelContextWindow")
        kind, name = "context", "token usage"
    if typ in ("turn_context", "session_meta") or channel in ("launch", "stdin"):
        kind = "context"
    if typ == "system" and obj.get("subtype") in ("compact_boundary", "microcompact_boundary"):
        kind, name = "context", obj["subtype"]
    if not text:
        text = json.dumps(p, ensure_ascii=False, indent=2)
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, indent=2)
    return {"seq": seq, "ts": ts, "kind": kind, "name": name, "text": text,
            "call_id": call_id, "context": context, "window": window,
            "elapsed_ns": raw.get("elapsed_ns"), "raw": raw}


@functools.lru_cache(maxsize=2)
def read_source(path, size, modified):
    # Signature invalidates live files; retain a bounded number of sources.
    events, points = [], []
    for obj in records(pathlib.Path(path)):
        event = normalize(obj, len(events) + 1)
        events.append(event)
        if event["context"] is not None:
            points.append({k: event[k] for k in ("seq", "ts", "context", "window")})
    return events, points


def source_data(path):
    st = path.stat()
    return read_source(str(path), st.st_size, st.st_mtime_ns)


@functools.lru_cache(maxsize=2)
def source_view(path, size, modified):
    from .debugview import build
    events, _ = read_source(path, size, modified)
    return build(events)


def overview(sessions):
    all_runs = [r for s in sessions.values() for r in s["runs"]]
    days, engines, tools = {}, {}, {}
    for r in all_runs:
        day = r["ts"][:10]
        d = days.setdefault(day, {"date": day, "runs": 0, "duration_ms": 0, "tokens": 0, "errors": 0})
        d["runs"] += 1
        d["duration_ms"] += r["duration_ms"] or 0
        d["tokens"] += (r["input_tokens"] or 0) + (r["output_tokens"] or 0)
        d["errors"] += bool(r["error"])
        e = engines.setdefault(r["engine"], {"engine": r["engine"], "runs": 0, "duration_ms": 0})
        e["runs"] += 1
        e["duration_ms"] += r["duration_ms"] or 0
    tools = rows("SELECT name, COUNT(*) calls, SUM(ms) ms, AVG(ms) avg_ms FROM run_phases WHERE kind='tool' GROUP BY name ORDER BY ms DESC LIMIT 15")
    durations = sorted(r["duration_ms"] for r in all_runs if r["duration_ms"] is not None)
    return {"sessions": len(sessions), "runs": len(all_runs), "errors": sum(bool(r["error"]) for r in all_runs),
            "duration_ms": sum(durations), "median_ms": durations[len(durations)//2] if durations else None,
            "p95_ms": durations[min(len(durations)-1, int(len(durations)*.95))] if durations else None,
            "input_tokens": sum(r["input_tokens"] or 0 for r in all_runs),
            "output_tokens": sum(r["output_tokens"] or 0 for r in all_runs),
            "cache_read": sum(r["cache_read"] or 0 for r in all_runs),
            "model_ms": sum(r["model_ms"] or 0 for r in all_runs),
            "tool_ms": sum(r["tool_ms"] or 0 for r in all_runs),
            "startup_ms": sum(r["startup_ms"] or 0 for r in all_runs),
            "cost_usd": sum(r["cost_usd"] or 0 for r in all_runs),
            "priced_runs": sum(r["cost_usd"] is not None for r in all_runs),
            "days": sorted(days.values(), key=lambda d:d["date"]), "engines": list(engines.values()), "tools": tools}


def make_server(port=None):
    catalog = Catalog()
    clean = redactor()
    port = config.get("debug.port", 8795) if port is None else port
    public = urlsplit(config.get("debug.url") or "")
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if public.netloc:
        hosts.add(public.netloc)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never put transcripts, queries or identifiers in access logs.

        def reply(self, status, data, content_type="application/json; charset=utf-8", filename=None):
            if not isinstance(data, bytes):
                data = json.dumps(clean(data), ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            if host not in hosts or (origin and urlsplit(origin).netloc != host):
                return self.reply(403, {"error": "Origin or host refused"})
            url = urlsplit(self.path)
            q = parse_qs(url.query)
            try:
                if url.path in ("/", "/app.js", "/style.css"):
                    filename = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[url.path]
                    mime = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[filename]
                    return self.reply(200, (ASSETS / filename).read_bytes(), mime + "; charset=utf-8")
                sessions = catalog.get()
                if url.path == "/api/sessions":
                    result = [{k:v for k,v in s.items() if k not in ("sources", "runs")} for s in sessions.values()]
                    result.sort(key=lambda s:s["updated"] or "", reverse=True)
                    return self.reply(200, {"sessions": result, "timezone": str(config.tz())})
                if url.path == "/api/overview":
                    return self.reply(200, overview(sessions))
                parts = url.path.strip("/").split("/")
                if len(parts) != 4 or parts[:2] != ["api", "session"]:
                    return self.reply(404, {"error": "Not found"})
                sid, action = unquote(parts[2]), parts[3]
                if not SID.fullmatch(sid) or sid not in sessions:
                    return self.reply(404, {"error": "Session not found"})
                s = sessions[sid]
                if action == "info":
                    ids = [r["id"] for r in s["runs"]]
                    phases = rows("SELECT * FROM run_phases WHERE run_id IN (" + ",".join("?" for _ in ids) + ") ORDER BY run_id,seq", ids) if ids else []
                    return self.reply(200, {"session": {k:v for k,v in s.items() if k != "sources"},
                                            "sources": [{"index": i, "kind": kind, "name": path.name, "bytes": path.stat().st_size} for i,(kind,path) in enumerate(s["sources"])], "phases": phases})
                index = int(q.get("source", ["0"])[0])
                if not 0 <= index < len(s["sources"]):
                    return self.reply(404, {"error": "No transcript available for this run"})
                _, path = s["sources"][index]
                if action == "download":
                    # Export every recorded line, without normalized duplication.
                    body = "".join(json.dumps(clean(obj), ensure_ascii=False) + "\n" for obj in records(path)).encode()
                    return self.reply(200, body, "application/x-ndjson", "session.jsonl")
                if action not in ("events", "analysis"):
                    return self.reply(404, {"error": "Not found"})
                events, points = source_data(path)
                view = None
                if action == "analysis" or q.get("view", [""])[0] == "activity":
                    stat = path.stat()
                    view = source_view(str(path), stat.st_size, stat.st_mtime_ns)
                    if action == "analysis":
                        return self.reply(200, {k:v for k,v in view.items() if k != "activities"})
                    events, points = view["activities"], view["context"]
                    turn = q.get("turn", [""])[0]
                    events = [e for e in events if (not turn or e["turn"] == turn) and
                              (q.get("technical", [""])[0] == "1" or not e["technical"])]
                kind, needle = q.get("kind", [""])[0], q.get("q", [""])[0].lower()
                filtered = [e for e in events if (not kind or e["kind"] == kind) and (not needle or needle in e["text"].lower() or needle in str(e.get("raw",e.get("records"))).lower())]
                offset, limit = max(0,int(q.get("offset", ["0"])[0])), min(250,max(1,int(q.get("limit", ["100"])[0])))
                if q.get("anchor"):
                    anchor = int(q["anchor"][0])
                    position = next((i for i,e in enumerate(filtered) if e["seq"] == anchor), None)
                    if position is not None:
                        offset = position // limit * limit
                return self.reply(200, {"events": filtered[offset:offset+limit], "total": len(filtered), "all": len(events), "offset": offset,
                                       "context": points, "bytes": path.stat().st_size})
            except (ValueError, TypeError):
                self.reply(400, {"error": "Invalid request"})
            except (OSError, sqlite3.Error):
                self.reply(503, {"error": "Source temporarily unavailable"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    hosts.update({f"127.0.0.1:{server.server_port}", f"localhost:{server.server_port}"})
    return server


def serve(port=None):
    server = make_server(port)
    print(f"Herald debug listening on http://127.0.0.1:{server.server_port}", flush=True)
    server.serve_forever()
