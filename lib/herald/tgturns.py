"""Durable Telegram work, saved before polling acknowledges the request.

The delivery outbox protects answers. This journal protects work that has not
answered yet. Native session IDs are checkpointed before tools run. Recovery
continues the transcript; it never replays recorded tool calls or approvals.
Private turns must never enter this journal.
"""
from __future__ import annotations

import json
import time
import uuid

from . import db, tgsessions

MAX_RECOVERIES = 3


def receive(update: dict, topic: str) -> str | None:
    identifier = f"update:{update['update_id']}" if update.get('update_id', -1) >= 0 else f"local:{uuid.uuid4().hex}"
    with db.session() as con:
        con.execute("""INSERT OR IGNORE INTO telegram_turns
            (id,topic,payload,created_at,updated_at) VALUES (?,?,?,?,?)""",
                    (identifier, topic, json.dumps(update), time.time(), time.time()))
        row = con.execute('SELECT status FROM telegram_turns WHERE id=?', (identifier,)).fetchone()
    return identifier if row['status'] in ('queued', 'running') else None


def begin(update: dict, topic: str, snapshot: dict) -> str:
    ids = update.get('_turn_ids') or [receive(update, topic)]
    ids = [i for i in ids if i]
    root = ids[0]
    payload = {k: v for k, v in update.items() if k != '_recover'}
    payload['_turn_ids'] = ids
    with db.session() as con:
        con.execute("""UPDATE telegram_turns SET status='running',payload=?,snapshot=?,session_id=COALESCE(?,session_id),
            attempts=attempts+1,updated_at=?,error=NULL WHERE id=?""",
                    (json.dumps(payload), json.dumps(snapshot), snapshot.get('session_id'), time.time(), root))
        for child in ids[1:]:
            con.execute("UPDATE telegram_turns SET parent_id=?,status='attached' WHERE id=?", (root, child))
    update['_turn_ids'] = ids
    return root


def session(root: str, session_id: str, snapshot: dict, chat_id: int, message_ids=()) -> None:
    snapshot = dict(snapshot, session_id=session_id)
    with db.session() as con:
        con.execute('UPDATE telegram_turns SET session_id=?,snapshot=?,updated_at=? WHERE id=?',
                    (session_id, json.dumps(snapshot), time.time(), root))
        row = con.execute('SELECT topic FROM telegram_turns WHERE id=?', (root,)).fetchone()
        tgsessions.save(con, session_id, row['topic'], engine=snapshot.get('engine'),
                        model=snapshot.get('model'), turns=snapshot.get('turns', 0),
                        orientation=snapshot.get('orientation'))
        tgsessions.record(con, chat_id, message_ids, session_id)


def progress(root: str, chat_id: int, message_ids) -> None:
    ids = list(dict.fromkeys(i for i in message_ids if i))
    with db.session() as con:
        con.execute('UPDATE telegram_turns SET messages=? WHERE id=?', (json.dumps(ids), root))
        row = con.execute('SELECT session_id FROM telegram_turns WHERE id=?', (root,)).fetchone()
        if row and row['session_id']:
            tgsessions.record(con, chat_id, ids, row['session_id'])


def attach(root: str, ids: list[str]) -> bool:
    with db.session() as con:
        row = con.execute('SELECT status FROM telegram_turns WHERE id=?', (root,)).fetchone()
        if not row or row['status'] != 'running':
            return False
        for identifier in ids:
            con.execute("UPDATE telegram_turns SET parent_id=?,status='attached' WHERE id=? AND status='queued'",
                        (root, identifier))
    return True


def detach(ids: list[str]) -> None:
    with db.session() as con:
        for identifier in ids:
            con.execute("UPDATE telegram_turns SET parent_id=NULL,status='queued' WHERE id=? AND status='attached'", (identifier,))


def answer(root: str, result: dict, snapshot: dict) -> None:
    with db.session() as con:
        con.execute('UPDATE telegram_turns SET answer=?,snapshot=?,session_id=COALESCE(?,session_id),updated_at=? WHERE id=?',
                    (json.dumps(result), json.dumps(snapshot), result.get('session_id'), time.time(), root))


def finish_in(con, root: str, *, paused: bool = False) -> None:
    """Commit in the same transaction as queuing the answer, before network I/O."""
    con.execute("""UPDATE telegram_turns SET status=?,payload='{}',answer=NULL,
        updated_at=? WHERE (id=? OR parent_id=?) AND status!='done'""",
                ('paused' if paused else 'done', time.time(), root, root))


def acknowledge(update: dict) -> None:
    """Non-model messages are consumed; attached steering belongs to its root."""
    with db.session() as con:
        for identifier in update.get('_turn_ids', []):
            con.execute("UPDATE telegram_turns SET status='done',payload='{}' WHERE id=? AND status='queued'",
                        (identifier,))


def pause(root: str, error: str) -> None:
    with db.session() as con:
        con.execute("UPDATE telegram_turns SET status='paused',error=?,updated_at=? WHERE id=? AND status!='done'",
                    (error[:500], time.time(), root))


def cancel(topic: str) -> None:
    with db.session() as con:
        con.execute("""UPDATE telegram_turns SET status='done',payload='{}',answer=NULL,
            updated_at=? WHERE topic=? AND status!='done'""", (time.time(), topic))


def supersede_paused(topic: str) -> None:
    with db.session() as con:
        con.execute("UPDATE telegram_turns SET status='done',payload='{}',answer=NULL WHERE topic=? AND status='paused'", (topic,))


def unfinished() -> list[dict]:
    with db.session() as con:
        return [dict(r) for r in con.execute("""SELECT * FROM telegram_turns
            WHERE parent_id IS NULL AND status IN ('queued','running','paused')
            ORDER BY created_at,id""")]


def coalesce_queued() -> None:
    """Rebuild albums/split pastes if the bridge died during their short timer."""
    rows = [r for r in unfinished() if r['status'] == 'queued' and not r['attempts']]
    used = set()
    for row in rows:
        if row['id'] in used:
            continue
        update = json.loads(row['payload'])
        msg = update.get('message') or {}
        gid = msg.get('media_group_id')
        text = msg.get('text') or ''
        group = [row]
        if gid:
            group += [r for r in rows if r['id'] != row['id'] and r['topic'] == row['topic']
                      and (json.loads(r['payload']).get('message') or {}).get('media_group_id') == gid]
            update['_album'] = [json.loads(r['payload'])['message'] for r in group]
        elif len(text) >= 3800 and not text.startswith('/'):
            last = row
            for candidate in rows[rows.index(row) + 1:]:
                if candidate['topic'] != row['topic']:
                    continue
                cm = json.loads(candidate['payload']).get('message') or {}
                ct = cm.get('text') or ''
                if (candidate['created_at'] - last['created_at'] > 2.5 or not ct or ct.startswith('/')
                        or any(cm.get(k) for k in ('document', 'photo', 'voice', 'audio', 'media_group_id'))):
                    break
                group.append(candidate)
                last = candidate
                if len(ct) < 3800:
                    break
            update['message'] = dict(msg, text='\n'.join(json.loads(r['payload'])['message']['text'] for r in group))
        if len(group) < 2:
            continue
        update['_turn_ids'] = [r['id'] for r in group]
        with db.session() as con:
            con.execute('UPDATE telegram_turns SET payload=? WHERE id=?', (json.dumps(update), row['id']))
            for child in group[1:]:
                con.execute("UPDATE telegram_turns SET parent_id=?,status='attached' WHERE id=?", (row['id'], child['id']))
        used.update(r['id'] for r in group)


def recovery_update(row: dict) -> dict:
    update = json.loads(row['payload'])
    update['_turn_ids'] = [row['id']]
    update['_recover'] = True
    with db.session() as con:
        children = [dict(r) for r in con.execute(
            "SELECT id,payload FROM telegram_turns WHERE parent_id=? AND status='attached' ORDER BY created_at,id", (row['id'],))]
    update['_turn_ids'] += [c['id'] for c in children]
    # Parts already merged into the root are not additional steering.
    original_ids = json.loads(row['payload']).get('_turn_ids', [])
    update['_recovery_followups'] = update.get('_recovery_followups', []) + [
        json.loads(c['payload']) for c in children if c['id'] not in original_ids]
    return update


def get(root: str) -> dict | None:
    with db.session() as con:
        row = con.execute('SELECT * FROM telegram_turns WHERE id=?', (root,)).fetchone()
        return dict(row) if row else None
