"""Full-text search over what Herald has written, read and been told.

A separate, disposable database: everything in it can be rebuilt, so it is not
memory and it does not need the ledger's migrations, change history or writer
lock. `herald collect search_index` keeps it current without a model. Exact
questions about dates and status still belong to `facts.db`; this answers "where
did we talk about that", which a query cannot unless you know what to ask for.

Keyword search misses paraphrases. Embeddings stay deferred until the evaluation
shows that costing answers.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re
import sqlite3

from . import config, mailtext, transcripts

ROOTS = ("identity", "state", "journal", "documents/archive", "documents/development")
MAX_FILE = 2_000_000
MAX_CHUNK = 6_000

SCHEMA = """
PRAGMA journal_mode=DELETE;
CREATE TABLE IF NOT EXISTS docs (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    ts TEXT,
    ref TEXT,
    digest TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_docs_kind ON docs(kind, ts);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    title, body, tokenize = 'porter unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, stamp TEXT NOT NULL);
"""

# (source, kind) -> what a result is called. Health readings, scraped listings
# and scored candidates are left out: a keyword match in those is noise.
FACTS = {
    ("gmail", "message"): "mail", ("purelymail", "message"): "mail",
    ("mail_reading", "message"): "reading",
    ("textview", "message"): "message", ("discord", "message"): "chat",
    ("canvas", "announcement"): "course", ("canvas", "assignment"): "course",
    ("contacts", "person"): "person", ("gtasks", "task"): "task",
    ("gcal", "event"): "event",
}
WEIGHT = {"note": 1.5, "reading": 1.4, "journal": 1.3, "conversation": 1.2, "person": 1.2,
          "course": 1.1, "event": 1.0, "mail": 1.0, "task": 1.0, "message": 0.8, "chat": 0.5}
KINDS = sorted(WEIGHT)

_SHAPES = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,})", re.S)
_ASSIGNED = re.compile(r"(?i)\b(password|passwd|secret|token|api[_ -]?key)\b(\s*[:=]\s*)\S{6,}")


def _values(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from _values(value)
    elif isinstance(node, list):
        for value in node:
            yield from _values(value)
    elif isinstance(node, str) and len(node) >= 8:
        yield node


def redactor():
    """A function that strips this install's secrets and anything shaped like one."""
    known = sorted(set(_values(config.secrets())), key=len, reverse=True)

    def clean(text: str) -> str:
        for value in known:
            if value in text:
                text = text.replace(value, "[redacted]")
        text = _SHAPES.sub("[redacted]", text)
        return _ASSIGNED.sub(r"\1\2[redacted]", text)
    return clean


def path() -> pathlib.Path:
    return config.LEDGER / "search.db"


def connect(*, write: bool = False) -> sqlite3.Connection:
    if write:
        con = sqlite3.connect(path(), timeout=30)
        con.executescript(SCHEMA)
        path().chmod(0o600)
    else:
        # Read-only so a private session, whose filesystem is read-only, can use it.
        con = sqlite3.connect(f"file:{path()}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _digest(*parts) -> str:
    return hashlib.sha256("\0".join(str(p) for p in parts).encode(errors="replace")).hexdigest()[:24]


def _put(con, key, kind, ts, ref, title, body) -> None:
    con.execute("DELETE FROM docs_fts WHERE rowid=(SELECT id FROM docs WHERE key=?)", (key,))
    con.execute("DELETE FROM docs WHERE key=?", (key,))
    cur = con.execute("INSERT INTO docs(key,kind,ts,ref,digest) VALUES (?,?,?,?,?)",
                      (key, kind, ts, ref, _digest(title, body)))
    con.execute("INSERT INTO docs_fts(rowid,title,body) VALUES (?,?,?)", (cur.lastrowid, title, body))


def _drop(con, keys) -> None:
    for key in keys:
        con.execute("DELETE FROM docs_fts WHERE rowid=(SELECT id FROM docs WHERE key=?)", (key,))
        con.execute("DELETE FROM docs WHERE key=?", (key,))


def _chunks(text: str):
    """Long sections split at paragraphs, so a match has useful surroundings."""
    while len(text) > MAX_CHUNK:
        cut = text.rfind("\n\n", 0, MAX_CHUNK)
        cut = cut if cut > MAX_CHUNK // 2 else MAX_CHUNK
        yield text[:cut]
        text = text[cut:]
    yield text


def _index_files(con, clean) -> int:
    from .context import sections
    seen, changed = set(), 0
    known = {r["path"]: r["stamp"] for r in con.execute("SELECT * FROM files")}
    for name in ROOTS:
        root = config.LEDGER / name
        if not root.is_dir():
            continue
        for file in sorted(root.rglob("*.md")):
            rel = file.relative_to(config.LEDGER).as_posix()
            if rel == "state/orientation.md" or file.is_symlink():
                continue
            try:
                stat = file.stat()
            except OSError:
                continue
            if stat.st_size > MAX_FILE:
                continue
            seen.add(rel)
            stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
            if known.get(rel) == stamp:
                continue
            _drop(con, [r[0] for r in con.execute("SELECT key FROM docs WHERE key LIKE ?", (f"file:{rel}#%",))])
            kind = "journal" if rel.startswith("journal/") else "note"
            ts = rel[8:18] if kind == "journal" else dt.datetime.fromtimestamp(stat.st_mtime).date().isoformat()
            parts = sections(file)
            lines = file.read_text(errors="replace").splitlines(keepends=True)
            # A parent section contains its children, so each heading indexes
            # only the text up to the next heading of any level.
            for s, after in zip(parts, [*parts[1:], None]):
                own = "".join(lines[s["line"] - 1:after["line"] - 1 if after else len(lines)])
                for n, chunk in enumerate(_chunks(own)):
                    _put(con, f"file:{rel}#{s['key']}.{n}", kind, ts,
                         f"herald context --file {rel} --section {s['key']} --budget 0",
                         f"{file.stem}: {s['heading']}", clean(chunk))
            con.execute("INSERT OR REPLACE INTO files VALUES (?,?)", (rel, stamp))
            changed += 1
    gone = set(known) - seen
    for rel in gone:
        _drop(con, [r[0] for r in con.execute("SELECT key FROM docs WHERE key LIKE ?", (f"file:{rel}#%",))])
        con.execute("DELETE FROM files WHERE path=?", (rel,))
    return changed


def _index_facts(con, ledger, clean) -> int:
    known = {r["key"]: r["digest"] for r in con.execute("SELECT key,digest FROM docs WHERE key LIKE 'fact:%'")}
    seen, changed = set(), 0
    for (source, kind), label in FACTS.items():
        for r in ledger.execute("SELECT id,external_id,ts,title,body,data FROM facts "
                                "WHERE source=? AND kind=?", (source, kind)):
            if label == "event":
                try:
                    if json.loads(r["data"] or "{}").get("role") != "mine":
                        continue
                except ValueError:
                    continue
            key = f"fact:{source}:{kind}:{r['external_id'] or r['id']}"
            seen.add(key)
            raw = (r["title"] or "", r["body"] or "")
            digest = _digest(*raw)
            if known.get(key) == digest:
                continue
            body = mailtext.compact(raw[1]) if label == "mail" else raw[1]
            _put(con, key, label, r["ts"], f"herald fact {r['id']}", clean(raw[0]), clean(body))
            con.execute("UPDATE docs SET digest=? WHERE key=?", (digest, key))
            changed += 1
    _drop(con, set(known) - seen)
    return changed


def _index_conversations(con, ledger, clean) -> int:
    sessions = {r[0] for r in ledger.execute(
        "SELECT DISTINCT session_id FROM runs WHERE label LIKE 'telegram%' AND session_id IS NOT NULL")}
    known = {r["path"]: r["stamp"] for r in con.execute("SELECT * FROM files WHERE path LIKE 'session:%'")}
    changed = 0
    for sid, file in transcripts.locate(sessions).items():
        stat = file.stat()
        stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
        if known.get(f"session:{sid}") == stamp:
            continue
        for n, turn in enumerate(transcripts.turns(file)):
            key = f"conv:{sid}:{n}"
            title = " ".join(turn["user"].split())[:100]
            body = f"{turn['user']}\n\n{turn['reply']}"
            if con.execute("SELECT 1 FROM docs WHERE key=? AND digest=?", (key, _digest(clean(title), clean(body)))).fetchone():
                continue
            _put(con, key, "conversation", (turn["ts"] or "")[:19], f"herald search --show {key}",
                 clean(title), clean(body))
            changed += 1
        con.execute("INSERT OR REPLACE INTO files VALUES (?,?)", (f"session:{sid}", stamp))
    return changed


def refresh(ledger) -> dict:
    clean = redactor()
    con = connect(write=True)
    try:
        counts = {"notes": _index_files(con, clean), "facts": _index_facts(con, ledger, clean),
                  "conversation turns": _index_conversations(con, ledger, clean)}
        con.execute("INSERT OR REPLACE INTO files VALUES ('index:updated',?)",
                    (dt.datetime.now(config.tz()).isoformat(timespec="minutes"),))
        con.commit()
    finally:
        con.close()
    return counts


def _terms(query: str) -> list[str]:
    phrases = re.findall(r'"([^"]+)"', query)
    rest = re.sub(r'"[^"]+"', " ", query)
    words = [w for w in re.findall(r"[^\W_]+", rest) if len(w) > 1 or w.isdigit()]
    return ['"' + p.replace('"', "") + '"' for p in phrases] + [f'"{w}"' for w in words]


def _score(row, today) -> float:
    try:
        age = max(0, (today - dt.date.fromisoformat((row["ts"] or "")[:10])).days)
    except ValueError:
        age = 365
    recency = 0.7 + 0.3 / (1 + age / 120)
    return row["rank"] * WEIGHT.get(row["kind"], 1.0) * recency


def find(query: str, *, kinds=None, since=None, until=None, limit=8) -> dict:
    """Ranked matches. Every word first, then any word if that finds too little."""
    terms = _terms(query)
    if not terms:
        raise ValueError("search needs at least one word")
    if not path().exists():
        raise ValueError("no search index yet; run `herald collect search_index`")
    today = dt.datetime.now(config.tz()).date()
    con = connect()
    try:
        where, params = [], []
        if kinds:
            where.append("d.kind IN (" + ",".join("?" for _ in kinds) + ")")
            params.extend(kinds)
        if since:
            where.append("d.ts>=?")
            params.append(since)
        if until:
            where.append("d.ts<=?")
            params.append(until + "￿")
        extra = "".join(" AND " + w for w in where)
        sql = ("SELECT d.key,d.kind,d.ts,d.ref,docs_fts.title,"
               "snippet(docs_fts,1,'«','»',' … ',28) snip,bm25(docs_fts,4.0,1.0) rank "
               f"FROM docs_fts JOIN docs d ON d.id=docs_fts.rowid WHERE docs_fts MATCH ?{extra} "
               "ORDER BY rank LIMIT 80")
        rows = con.execute(sql, [" AND ".join(terms), *params]).fetchall()
        matched = "all words"
        if len(rows) < limit and len(terms) > 1:
            seen = {r["key"] for r in rows}
            more = [r for r in con.execute(sql, [" OR ".join(terms), *params]) if r["key"] not in seen]
            ranked = sorted(rows, key=lambda r: _score(r, today))
            ranked += sorted(more, key=lambda r: _score(r, today))
            matched = "all words, then any word" if rows else "any word"
        else:
            ranked = sorted(rows, key=lambda r: _score(r, today))
        stamp = con.execute("SELECT stamp FROM files WHERE path='index:updated'").fetchone()
        return {"results": [dict(r) for r in ranked[:limit]], "total": len(ranked),
                "matched": matched, "updated": stamp[0] if stamp else None}
    finally:
        con.close()


def show(key: str) -> str:
    con = connect()
    try:
        row = con.execute("SELECT d.ts,docs_fts.title,docs_fts.body FROM docs d "
                          "JOIN docs_fts ON docs_fts.rowid=d.id WHERE d.key=?", (key,)).fetchone()
    finally:
        con.close()
    if row is None:
        raise ValueError("no such search result")
    return f"{row['ts']}\n{row['body']}\n"


def render(found: dict, budget: int = 1500) -> str:
    out = [f"{len(found['results'])} of {found['total']} matches ({found['matched']}); "
           f"index updated {found['updated'] or 'unknown'}. "
           "Keyword search: no match does not establish absence."]
    cap = budget * 4
    for r in found["results"]:
        date = (r["ts"] or "")[:10]
        block = f"\n[{r['kind']} {date}] {' '.join((r['title'] or '').split())[:110]}\n" \
                f"{' '.join((r['snip'] or '').split())}\nRead: {r['ref']}\n"
        if len("\n".join(out).encode()) + len(block.encode()) > cap:
            out.append("\n[more matches omitted by the budget; narrow with --kind or --since]")
            break
        out.append(block)
    return "".join(x if x.startswith("\n") else x + "\n" for x in out)
