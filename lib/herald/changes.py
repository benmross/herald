"""Substantive ledger revisions, independent of source row IDs.

SQLite triggers also cover direct SQL writers and source deletions. Snapshots
retain evidence for removals; read-time coalescing makes rebuilds and reverted
edits silent. Source ingestion timestamps alone never constitute a revision.
No model calls, external writes, or source content in the document index.

Reconcile snapshots at the writer to prevent rebuild churn. Suppress net-zero
changes only relative to a reader's cursor; intermediate readers may have seen
a temporary state. Retention bounds history and warns readers after expiry.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json

from . import config, private

TRIGGER_VERSION = "2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_changes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    entity TEXT NOT NULL,
    record_key TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_changes_entity
    ON ledger_changes(entity, record_key, seq);
CREATE TABLE IF NOT EXISTS memory_documents (
    path TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_metadata (key TEXT PRIMARY KEY, value TEXT);
"""

FIELDS = {
    "facts": ("source", "kind", "external_id", "ts", "title", "body", "data"),
    "commitments": ("text", "kind", "due", "status", "notes"),
    "deadlines": ("source", "ref", "course", "kind", "title", "due", "end_at",
                  "notes", "status"),
    # The tables that own lifecycle status. A rejection recorded by another
    # session has to reach a conversation the same way a moved deadline does.
    "opportunities": ("title", "org", "kind", "deadline", "url", "fit", "score",
                      "status", "notes"),
    "applications": ("company", "title", "url", "status", "submitted_at", "notes"),
}


def install(con) -> None:
    """Fresh and upgraded ledgers share this idempotent setup; no backfill."""
    con.executescript(SCHEMA)
    row = con.execute("SELECT value FROM memory_metadata WHERE key='trigger_version'").fetchone()
    if row and row[0] == TRIGGER_VERSION:
        return
    for table, fields in FIELDS.items():
        columns = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if not columns:
            continue
        fields = tuple(f for f in fields if f in columns)

        def snapshot(prefix):
            return "json_object(" + ",".join(
                f"'{f}',{prefix}.{f}" for f in fields) + ")"

        def key(prefix):
            if table == "facts" and "external_id" in columns:
                return (f"json_array({prefix}.source,{prefix}.kind,"
                        f"{prefix}.external_id, CASE WHEN {prefix}.external_id IS NULL THEN {prefix}.id END)")
            return f"CAST({prefix}.id AS TEXT)"

        for operation, before, after in (
                ("insert", "NULL", snapshot("NEW")),
                ("update", snapshot("OLD"), snapshot("NEW")),
                ("delete", snapshot("OLD"), "NULL")):
            prefix = "OLD" if operation == "delete" else "NEW"
            when = (" WHEN " + " OR ".join(f"OLD.{f} IS NOT NEW.{f}" for f in fields)
                    if operation == "update" else "")
            con.execute(f"DROP TRIGGER IF EXISTS memory_{table}_{operation}")
            con.executescript(f"""
                CREATE TRIGGER IF NOT EXISTS memory_{table}_{operation}
                AFTER {operation.upper()} ON {table}{when}
                BEGIN
                  INSERT INTO ledger_changes(entity,record_key,before_json,after_json)
                  VALUES ('{table}',{key(prefix)},{before},{after});
                END;
            """)
    con.execute("INSERT OR REPLACE INTO memory_metadata VALUES ('trigger_version',?)",
                (TRIGGER_VERSION,))
    con.commit()


def sequence(con) -> int:
    # AUTOINCREMENT prevents cursor reuse if revisions are archived later.
    row = con.execute("SELECT seq FROM sqlite_sequence WHERE name='ledger_changes'").fetchone()
    return row[0] if row else 0


def _canonical(raw):
    value = json.loads(raw) if raw else None
    if isinstance(value, dict) and isinstance(value.get("data"), str):
        try:
            value["data"] = json.loads(value["data"])
        except (ValueError, TypeError):
            pass
    return value


def net(con, since: int, through: int | None = None) -> list[dict]:
    """Latest state against earliest previous state, not an ID subtraction."""
    grouped = {}
    for r in con.execute("SELECT * FROM ledger_changes WHERE seq>? AND seq<=? ORDER BY seq",
                         (since, through if through is not None else sequence(con))):
        key = (r["entity"], r["record_key"])
        if key not in grouped:
            grouped[key] = dict(r)
        else:
            grouped[key]["after_json"] = r["after_json"]
            grouped[key]["seq"] = r["seq"]
    out = []
    for row in grouped.values():
        before, after = _canonical(row["before_json"]), _canonical(row["after_json"])
        if before != after:
            row.update(before=before, after=after)
            out.append(row)
    return sorted(out, key=lambda r: r["seq"])


def compact(con, since: int = 0, through: int | None = None) -> int:
    """Compatibility for old migrations: retain revisions until expiry.

    A net-zero interval may contain a reader's cursor. Deleting A -> B -> A
    loses the correction for a reader that saw B. Only net(), relative to
    each reader, may suppress these transitions. Use replace_facts at writers
    to prevent rebuild churn, and prune() to bound retained history.
    """
    return 0


def prune(con, days: int) -> int:
    """Forget revisions older than `days`; current records are untouched."""
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    last = con.execute("SELECT MAX(seq) FROM ledger_changes WHERE changed_at<?", (cutoff,)).fetchone()[0]
    if last is None:
        return 0
    removed = con.execute("DELETE FROM ledger_changes WHERE seq<=?", (last,)).rowcount
    con.execute("INSERT INTO memory_metadata VALUES ('pruned_through',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(last),))
    return removed


def pruned_through(con) -> int:
    row = con.execute("SELECT value FROM memory_metadata WHERE key='pruned_through'").fetchone()
    return int(row[0]) if row else 0


def scan_documents(con) -> None:
    """Revision notices for maintained Markdown, including removals.

    The first scan is a baseline, not hundreds of announcements. Private mode
    never scans or writes. Missing/unreadable roots do not imply deletions.
    """
    if private.active():
        return
    baseline = con.execute("SELECT 1 FROM memory_metadata WHERE key='documents_baselined'").fetchone()
    known = {r["path"]: r for r in con.execute("SELECT * FROM memory_documents")}
    seen = set()
    roots = []
    for name in ("identity", "state", "journal"):
        root = config.LEDGER / name
        if not root.is_dir():
            continue
        roots.append(name + "/")
        for path in sorted(root.rglob("*.md")):
            rel = path.relative_to(config.LEDGER).as_posix()
            # Orientation is a derived view regenerated by session startup.
            if rel == "state/orientation.md" or path.is_symlink():
                continue
            seen.add(rel)
            old = known.get(rel)
            try:
                stat = path.stat()
                if old and (old["mtime_ns"], old["size"]) == (stat.st_mtime_ns, stat.st_size):
                    continue
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
            if baseline and (not old or digest != old["digest"]):
                con.execute("INSERT INTO ledger_changes(entity,record_key,before_json,after_json) "
                            "VALUES ('document',?,?,?)",
                            (rel, json.dumps({"path": rel, "digest": old["digest"]}) if old else None,
                             json.dumps({"path": rel, "digest": digest})))
            con.execute("INSERT INTO memory_documents VALUES (?,?,?,?) "
                        "ON CONFLICT(path) DO UPDATE SET digest=excluded.digest, "
                        "size=excluded.size,mtime_ns=excluded.mtime_ns",
                        (rel, digest, stat.st_size, stat.st_mtime_ns))
    for rel, old in known.items():
        if rel not in seen and any(rel.startswith(r) for r in roots):
            if baseline:
                con.execute("INSERT INTO ledger_changes(entity,record_key,before_json) "
                            "VALUES ('document',?,?)",
                            (rel, json.dumps({"path": rel, "digest": old["digest"]})))
            con.execute("DELETE FROM memory_documents WHERE path=?", (rel,))
    con.execute("INSERT OR IGNORE INTO memory_metadata VALUES ('documents_baselined','1')")


def health(con) -> dict:
    return {r["collector"]: {"status": r["sync_status"], "last_complete": r["last_complete"],
                              "pending": r["pending_retries"], "coverage": r["coverage"],
                              "reason": r["last_error"]}
            for r in con.execute("SELECT * FROM collector_state WHERE sync_status IN ('partial','failed')")}


def health_lines(states: dict) -> list[str]:
    return [f"- {name}: {s['status']} source coverage; {s['pending']} pending retries; "
            f"last complete sync {s['last_complete'] or 'not established'}. "
            "Do not treat missing records as proof that nothing exists."
            for name, s in sorted(states.items())]
