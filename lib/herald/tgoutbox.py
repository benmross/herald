"""Retain completed Telegram replies across outages and bridge restarts.

Only text delivery is retried here, never tools, model turns or approvals.
Telegram has no sendMessage idempotency key: an ambiguous network timeout can
still duplicate an accepted message. Chunk ordering and confirmed successes
are preserved, and HTTP rejections do not trigger a second plain-text send.
"""
from __future__ import annotations

import json
import sys
import time

from . import db, tgapi, tgsessions


def enqueue(method: str, params: dict, *, fallback: str | None = None,
            session_id: str | None = None, key: str | None = None) -> int:
    if method not in ("sendMessage", "editMessageText"):
        raise ValueError("Only text messages and progress edits may be queued")
    with db.session() as con:
        row = con.execute("""
            INSERT INTO telegram_outbox (created_at, method, params, fallback, session_id, dedupe_key)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(dedupe_key) DO UPDATE SET params=excluded.params
            RETURNING id
        """, (db.now(), method, json.dumps(params), fallback, session_id, key)).fetchone()
        return row[0]


def deliver(identifier: int, api) -> dict | None:
    now = time.time()
    with db.session() as con:
        # Claim before doing I/O; no database lock is held during a request.
        # A crashed worker's lease expires, so restart recovery needs no reset.
        row = con.execute("""
            UPDATE telegram_outbox SET next_attempt=?, attempts=attempts+1
            WHERE id=? AND state='pending' AND next_attempt<=?
              AND (method!='sendMessage' OR NOT EXISTS (
                SELECT 1 FROM telegram_outbox older
                WHERE older.id<telegram_outbox.id AND older.state='pending'
                  AND older.method='sendMessage'
                  AND json_extract(older.params, '$.chat_id')=json_extract(telegram_outbox.params, '$.chat_id')
                  AND json_extract(older.params, '$.message_thread_id') IS json_extract(telegram_outbox.params, '$.message_thread_id')))
            RETURNING *
        """, (now + 300, identifier, now)).fetchone()
    if row is None:
        return None
    params = json.loads(row['params'])
    response = api(row['method'], **params)
    if tgapi.formatting_error(response) and row['fallback'] is not None:
        params['text'] = row['fallback']
        params.pop('parse_mode', None)
        response = api(row['method'], **params)
    with db.session() as con:
        if response and response.get('ok'):
            result = response.get('result')
            mid = result.get('message_id') if isinstance(result, dict) else None
            if row['session_id'] and mid:
                tgsessions.record(con, params['chat_id'], [mid], row['session_id'])
            con.execute('DELETE FROM telegram_outbox WHERE id=?', (identifier,))
        elif tgapi.transient(response):
            delay = max(tgapi.retry_after(response), min(300, 5 * 2 ** min(row['attempts'], 6)))
            con.execute("UPDATE telegram_outbox SET params=?, next_attempt=?, last_error=? WHERE id=?",
                        (json.dumps(params), time.time() + delay,
                         str((response or {}).get('error_code', 'network/JSON')), identifier))
        else:
            con.execute("UPDATE telegram_outbox SET state='failed', last_error=? WHERE id=?",
                        (str((response or {}).get('error_code', 'unknown')), identifier))
            print(f"telegram delivery {identifier}: permanent rejection; retained for inspection",
                  file=sys.stderr, flush=True)
    return response


def retry(api) -> None:
    with db.session() as con:
        ids = [row[0] for row in con.execute(
            "SELECT id FROM telegram_outbox WHERE state='pending' AND next_attempt<=? ORDER BY id LIMIT 10",
            (time.time(),))]
    for identifier in ids:
        deliver(identifier, api)
