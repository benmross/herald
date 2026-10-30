"""Bounded topic context and reusable operational reads, without model calls.

This is a lookup interface, not semantic search or a second source of truth.
Only filenames and headings route notes. Operational tables own their statuses.
Metrics contain categories, sizes and timings, never queries or source text.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shlex
import sqlite3
import tempfile
import time

from . import changes, config, private

VIEWS = {
    'commitments': ('commitments', ('text', 'kind', 'due', 'status', 'notes', 'source_fact'),
                    "status='open'", "coalesce(due,'9999'),id", ('text', 'notes')),
    'deadlines': ('deadlines', ('title', 'course', 'kind', 'due', 'status', 'notes', 'source', 'ref'),
                  "status='live' AND due>=? AND due<?", 'due,id', ('title', 'course', 'notes')),
    'applications': ('applications', ('company', 'title', 'status', 'submitted_at', 'opportunity_id', 'url'),
                     '1', 'id DESC', ('company', 'title')),
    'opportunities': ('opportunities', ('title', 'org', 'kind', 'status', 'deadline', 'score', 'url', 'source_fact'),
                      "status IN ('new','surfaced','interested') AND (deadline IS NULL OR deadline='' OR deadline>=?)",
                      "deadline IS NULL,deadline,score DESC,id DESC", ('title', 'org', 'notes', 'fit')),
    'schedule': ('facts', ('title', 'ts', 'data'),
                 "kind='event' AND json_extract(data,'$.role')='mine'", 'ts,id', ('title', 'body')),
}


def normalized(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', value.lower())


def ledger_path(value: str) -> pathlib.Path:
    path = pathlib.Path(value)
    if not path.is_absolute():
        parts = path.parts[1:] if path.parts[:1] == ('ledger',) else path.parts
        path = config.LEDGER.joinpath(*parts)
    path = path.resolve()
    if not path.is_relative_to(config.LEDGER.resolve()) or path.suffix != '.md':
        raise ValueError('choose a Markdown file inside the ledger')
    if not path.is_file():
        raise ValueError('that ledger Markdown file does not exist')
    return path


def sections(path: pathlib.Path) -> list[dict]:
    """ATX headings outside fenced code, including nested section boundaries."""
    lines = path.read_text().splitlines(keepends=True)
    headings, fence = [], None
    for i, line in enumerate(lines):
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        match = re.match(r'^(#{1,6})\s+(.+?)\s*#*\s*$', line) if fence is None else None
        if match:
            headings.append({'key': f's{i + 1}', 'heading': match[2],
                             'level': len(match[1]), 'line': i + 1})
    if not headings:
        headings = [{'key': 's1', 'heading': path.stem, 'level': 1, 'line': 1}]
    for pos, h in enumerate(headings):
        stop = next((x['line'] - 1 for x in headings[pos + 1:] if x['level'] <= h['level']), len(lines))
        h['end'] = stop
        h['text'] = ''.join(lines[h['line'] - 1:stop])
    return headings


def topics() -> list[dict]:
    out = []
    for folder in ('state/areas', 'state/projects', 'state/reference'):
        root = config.LEDGER / folder
        for path in sorted(root.glob('*.md')):
            if path.is_symlink():
                continue
            try:
                with path.open() as stream:
                    first = next((s.strip('# \n') for s in stream if s.startswith('# ')), path.stem)
                out.append({'topic': path.stem, 'title': first,
                            'path': path.relative_to(config.LEDGER).as_posix()})
            except OSError:
                continue
    for name in ('now', 'opportunities', 'commitments'):
        path = config.STATE / f'{name}.md'
        if path.is_file():
            out.append({'topic': name, 'title': name.title(), 'path': f'state/{name}.md'})
    return out


def resolve(topic: str) -> list[dict]:
    key = normalized(topic)
    index = topics()
    exact = [t for t in index if key in (normalized(t['topic']), normalized(t['title']))]
    if exact:
        return exact
    words = [normalized(w) for w in topic.split() if normalized(w)]
    return [t for t in index if words and all(w in normalized(t['topic'] + t['title']) for w in words)]


def _match(fields, topic, params):
    # Values remain parameters even when a topic contains SQL metacharacters.
    if not topic:
        return ''
    params.extend([normalized(topic)] * len(fields))
    expr = " OR ".join(f"replace(replace(replace(lower(coalesce({f},'')),' ',''),'-',''),'_','') LIKE '%' || ? || '%'" for f in fields)
    return ' AND (' + expr + ')'


def operational(con, view: str, *, topic='', days=30, offset=0, limit=10,
                record_id=None, all_statuses=False) -> dict:
    table, columns, where, order, fields = VIEWS[view]
    params = []
    today = dt.datetime.now(config.tz()).date()
    if record_id is not None:
        where, params = 'id=?', [record_id]
        if view == 'schedule':
            where += " AND kind='event'"
        columns = tuple(r[1] for r in con.execute(f'PRAGMA table_info({table})') if r[1] != 'id')
    elif all_statuses:
        where = "kind='event' AND json_extract(data,'$.role')='mine'" if view == 'schedule' else '1'
    elif view == 'deadlines':
        params = [today.isoformat(), (today + dt.timedelta(days=days)).isoformat()]
    elif view == 'opportunities':
        params = [today.isoformat()]
    if view == 'schedule' and not all_statuses and record_id is None:
        start = dt.datetime.combine(today, dt.time(), config.tz())
        end = dt.datetime.combine(today + dt.timedelta(days=days), dt.time(), config.tz())
        where += " AND ((length(ts)=10 AND ts>=? AND ts<?) OR (length(ts)>10 AND datetime(ts)>=datetime(?) AND datetime(ts)<datetime(?)))"
        params.extend([today.isoformat(), (today + dt.timedelta(days=days)).isoformat(), start.isoformat(), end.isoformat()])
    where += _match(fields, topic, params)
    count = con.execute(f'SELECT count(*) FROM {table} WHERE {where}', params).fetchone()[0]
    rows = con.execute(f"SELECT id,{','.join(columns)} FROM {table} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                       [*params, limit, offset]).fetchall()
    items = []
    for r in rows:
        value = dict(r)
        if view == 'schedule' and record_id is None:
            data = json.loads(value.pop('data') or '{}')
            value.update(role=data.get('role'), location=data.get('location'), end=data.get('end'))
        ref = f"{table}:{r['id']}"
        items.append({'source': ref, 'text': json.dumps(value, ensure_ascii=False),
                      'expand': f'herald context --view {view} --id {r["id"]} --budget 0'})
    return {'items': items, 'total': count, 'has_more': offset + len(items) < count}


def counts(con, topic='') -> list[dict]:
    out = []
    for view in ('commitments', 'deadlines', 'opportunities', 'applications'):
        table, _, _, _, fields = VIEWS[view]
        params = []
        where = '1' + _match(fields, topic, params)
        groups = {r['status']: r['n'] for r in con.execute(
            f'SELECT status,count(*) n FROM {table} WHERE {where} GROUP BY status', params)}
        out.append({'source': table, 'text': 'Status counts over all retained dates: ' + json.dumps(groups, sort_keys=True),
                    'expand': f'herald context --view {view} --all --budget 0'})
    return out


def _note(path, section=None, offset=0) -> dict:
    all_sections = sections(path)
    selected = all_sections
    if section:
        exact = [s for s in all_sections if s['key'] == section or s['heading'].casefold() == section.casefold()]
        selected = exact or [s for s in all_sections if section.casefold() in s['heading'].casefold()]
        if len(selected) != 1:
            raise ValueError('section is missing or ambiguous; use --sections and choose its sLINE key')
    else:
        # Intro only: nested policies are separate evidence and remain listed.
        first = dict(all_sections[0])
        next_line = all_sections[1]['line'] if len(all_sections) > 1 else first['end'] + 1
        first['end'] = next_line - 1
        first['text'] = ''.join(path.read_text().splitlines(keepends=True)[first['line']-1:first['end']])
        selected = [first]
    h = selected[0]
    content = h['text'].splitlines(keepends=True)
    if offset > len(content):
        raise ValueError('line offset is beyond the selected section')
    rel = path.relative_to(config.LEDGER.resolve()).as_posix()
    base = f'herald context --file {shlex.quote(rel)} --section {h["key"]}'
    return {'source': f'{rel}:{h["line"] + offset}',
            'text': 'Note modified ' + dt.datetime.fromtimestamp(path.stat().st_mtime, config.tz()).isoformat(timespec='seconds') + '\n' + ''.join(content[offset:]), 'expand': base + ' --budget 0',
            'continuation_base': base, 'line_offset': offset, 'prefix_lines': 1}


def packet(con, *, topic='', view=None, file=None, section=None, list_sections=False,
           days=30, offset=0, limit=10, record_id=None, all_statuses=False, schema=False):
    if topic and not normalized(topic):
        raise ValueError('topic must include a letter or number')
    if topic == 'opportunities' and not (file or section or list_sections or view):
        topic, view = '', 'opportunities'
    items, more, category = [], False, 'topic'
    routes = resolve(topic) if topic and topic not in ('topics', 'stats') else []
    if schema:
        category = 'schema'
        for name in ([view] if view and view != 'counts' else VIEWS):
            table = VIEWS[name][0]
            columns = ', '.join(r[1] for r in con.execute(f'PRAGMA table_info({table})'))
            items.append({'source': table, 'text': columns,
                          'expand': f'herald context --view {name}'})
    elif topic == 'topics' and not file and not view:
        category = 'index'
        index = topics()
        items = [{'source': t['path'], 'text': t['topic'] + ': ' + t['title'],
                  'expand': 'herald context ' + shlex.quote(t['topic'])} for t in index[offset:offset+limit]]
        more = offset + len(items) < len(index)
    elif view:
        category = view
        if view == 'counts':
            items = counts(con, topic)
        else:
            result = operational(con, view, topic=topic, days=days, offset=offset,
                                 limit=limit, record_id=record_id, all_statuses=all_statuses)
            items, more = result['items'], result['has_more']
            items.insert(0, {'source': view, 'text': f"{result['total']} matching records; showing from offset {offset}.", 'expand': ''})
    elif file or section or list_sections:
        category = 'sections' if list_sections else 'section'
        if file:
            path = ledger_path(file)
        elif len(routes) == 1:
            path = ledger_path(routes[0]['path'])
        else:
            raise ValueError('choose one topic or --file before selecting sections')
        if list_sections:
            rel = path.relative_to(config.LEDGER.resolve()).as_posix()
            index = sections(path)
            items = [{'source': f'{rel}:{s["line"]}', 'text': f'{s["key"]}: {s["heading"]} (lines {s["line"]}-{s["end"]})',
                      'expand': f'herald context --file {shlex.quote(rel)} --section {s["key"]}'} for s in index[offset:offset+limit]]
            more = offset + len(items) < len(index)
        else:
            items = [_note(path, section, offset)]
    else:
        if len(routes) > 1:
            raise ValueError('topic is ambiguous: ' + ', '.join(t['topic'] for t in routes) + '; choose --file')
        if routes:
            path = ledger_path(routes[0]['path'])
            items.append(_note(path))
            index = sections(path)
            for entry in [s for s in index[1:] if re.search(
                    r'\b(current|status|instructor|contact|constraints|overview)\b', s['heading'], re.I)][:2]:
                preview = _note(path, entry['key'])
                lines = preview['text'].splitlines()
                if len(lines) > 15:
                    preview['text'] = '\n'.join(lines[:15]) + '\n[section preview; use the source expansion for the rest]'
                items.append(preview)
            headings = '; '.join(f'{s["key"]} {s["heading"]}' for s in index[1:9])
            items.append({'source': routes[0]['path'], 'text': 'Section index (partial): ' + (headings or 'no additional headings'),
                          'expand': f'herald context --file {shlex.quote(routes[0]["path"])} --sections --limit 100'})
        elif topic:
            items.append({'source': 'topic index', 'text': 'No matching maintained topic file. Structured matches follow; no relationship or membership is inferred.',
                          'expand': 'herald context topics --limit 100'})
        items.extend(counts(con, topic))
        for name in ('commitments', 'deadlines', 'schedule', 'applications', 'opportunities'):
            result = operational(con, name, topic=topic, days=days, limit=2)
            if result['total']:
                items.append({'source': name, 'text': f"{result['total']} matching current records.",
                              'expand': f'herald context {shlex.quote(topic)} --view {name}'})
                items.extend(result['items'])
                more |= result['has_more']
    if category not in ('schema', 'index', 'sections'):
        sources = ('gcal', 'deadlines', 'applications', 'obligations', 'opportunities')
        freshness = [dict(r) for r in con.execute(
            "SELECT collector,sync_status,last_complete FROM collector_state WHERE collector IN (?,?,?,?,?)",
            sources)]
        if freshness:
            items.insert(0, {'source': 'source freshness', 'text': '\n'.join(
                f"{r['collector']}: {r['sync_status']}; last complete {r['last_complete'] or 'not established'}"
                for r in freshness),
                             'expand': 'herald status'})
    health = changes.health(con)
    notes = changes.health_lines(health)
    notes.append('Evidence is selective ledger data. Verify the full source for policy or submission decisions. Empty matches do not establish absence from an incomplete source.')
    return {'category': category, 'items': items, 'has_more': more, 'notes': notes,
            'incomplete_sources': sorted(health),
            'generated': dt.datetime.now(config.tz()).isoformat(timespec='seconds')}


def render(result: dict, budget=1500) -> tuple[str, int, bool]:
    """Budget in approximate tokens (UTF-8 bytes / 4), with explicit excerpts."""
    cap = budget * 4 if budget else None
    header = f"Context generated {result['generated']}\n"
    notes = '\n'.join(result['notes']) + '\n'
    if cap and len((header + notes).encode()) > cap // 2:
        notes = 'Selective evidence; verify originals. Incomplete source coverage may hide records.\n'
        sources = result.get('incomplete_sources', [])
        if sources:
            notes += 'Incomplete sources: ' + ', '.join(sources[:6]) + '; see herald status.\n'
    prefix = header + notes
    blocks, shown, more = [], 0, result['has_more']
    reserve = 250
    for item in result['items']:
        text = f"\n[{item['source']}]\n{item['text'].rstrip()}\n"
        expand = f"Read further: {item['expand']}\n" if item['expand'] else ''
        available = cap - len((prefix + ''.join(blocks)).encode()) - reserve if cap else None
        if available is None or len((text + expand).encode()) <= available:
            blocks.append(text + expand)
            shown += 1
            continue
        more = True
        if available > len(expand.encode()) + 180:
            extra = len(item.get('continuation_base', '').encode()) + 90 if item.get('continuation_base') else 0
            excerpt = text.encode()[:max(0, available - len(expand.encode()) - 110 - extra)].decode('utf-8', 'ignore')
            # Stop at a line boundary where possible; a very long line is
            # explicitly marked as an excerpt and has a whole-source command.
            if '\n' in excerpt[60:]:
                excerpt = excerpt.rsplit('\n', 1)[0]
            continued = ''
            if item.get('continuation_base'):
                complete_lines = max(0, len(excerpt.splitlines()) - 2 - item.get('prefix_lines', 0))
                if complete_lines:
                    next_offset = item.get('line_offset', 0) + complete_lines
                    continued = f"Continue: {item['continuation_base']} --offset {next_offset} --budget {budget}\n"
            blocks.append(excerpt + '\n[excerpt; remaining evidence omitted]\n' + expand + continued)
            shown += 1
        break
    if shown < len(result['items']):
        more = True
    footer = (f'\nShown {shown}/{len(result["items"])} selected items; more available: {str(more).lower()}. '
              'Use a listed expansion, --view, --sections or --offset for more.\n')
    output = prefix + ''.join(blocks) + footer
    return output, shown, more


def record(con, category, started, output, shown, more, parent=None):
    if private.active():
        return None
    turn = os.environ.get('HERALD_RETRIEVAL_TURN')
    turn_key = hashlib.sha256(turn.encode()).hexdigest() if turn else None
    if parent is None and turn_key:
        row = con.execute('SELECT id FROM retrievals WHERE turn_key=? ORDER BY id DESC LIMIT 1', (turn_key,)).fetchone()
        parent = row[0] if row else None
    size = len(output.encode())
    cur = con.execute('INSERT INTO retrievals(ts,category,elapsed_ms,output_bytes,estimated_tokens,item_count,has_more,turn_key,parent_id) VALUES (?,?,?,?,?,?,?,?,?)',
                      (dt.datetime.now(config.tz()).isoformat(), category, (time.perf_counter()-started)*1000,
                       size, (size+3)//4, shown, int(more), turn_key, parent))
    return cur.lastrowid


def stats(con) -> str:
    rows = con.execute('SELECT category,count(*) calls,round(avg(elapsed_ms),2) avg_ms,round(avg(output_bytes)) avg_bytes,sum(has_more) with_more,count(parent_id) followup_calls FROM retrievals GROUP BY category ORDER BY calls DESC').fetchall()
    return json.dumps([dict(r) for r in rows], indent=2) + '\n'


def refresh_opportunities(con) -> pathlib.Path | None:
    """Rebuild a small derived view; archive an older narrative without loss."""
    private.refuse('Refreshing the opportunities overview')
    path = config.STATE / 'opportunities.md'
    previous = path.read_bytes() if path.exists() else b''
    marker = '<!-- herald:opportunities-overview -->'
    archive = None
    if previous and marker.encode() not in previous:
        digest = hashlib.sha256(previous).hexdigest()[:16]
        archive = config.LEDGER / 'documents/archive' / f'opportunities-{digest}.md'
        archive.parent.mkdir(parents=True, exist_ok=True)
        if not archive.exists():
            with archive.open('xb') as target:
                target.write(previous)
            archive.chmod(0o600)
        elif archive.read_bytes() != previous:
            raise ValueError('existing opportunity archive does not match the original')
    generated = dt.datetime.now(config.tz()).isoformat(timespec='minutes')
    groups = {r['status']: r['n'] for r in con.execute('SELECT status,count(*) n FROM opportunities GROUP BY status')}
    lines = ['# Opportunities', '', marker, '', f'Generated {generated}. This is a selective overview; query current tables for status.', '',
             'Recorded status counts: ' + ', '.join(f'{status}: {n}' for status, n in sorted(groups.items())) + '.', '',
             'Use `herald context --view opportunities` for currently open rows and paging.',
             'Use `herald context --view applications` for application status; a saved draft is not a submission.',
             'Use `herald context --view counts` for all retained status counts.', '',
             '## Upcoming recorded deadlines', '']
    result = operational(con, 'opportunities', limit=8)
    for item in result['items']:
        r = json.loads(item['text'])
        if not r.get('deadline'):
            continue
        title = ' '.join(r['title'].split())[:180]
        org = ' '.join((r.get('org') or 'organization unspecified').split())[:80]
        lines.append(f"- {json.dumps(title, ensure_ascii=False)} ({json.dumps(org, ensure_ascii=False)}), {r['deadline']}. "
                     f"Details: `herald context --view opportunities --id {r['id']}`.")
    lines += ['', 'Full reasoning and notes remain in the opportunities table. Confirm the source before acting.',
              'Past scout decisions remain in daily journals. Earlier narrative overviews are preserved under `documents/archive/opportunities-*.md`.',
              'Do not append pass-by-pass history to this generated file.', '']
    output = '\n'.join(lines).encode()
    if marker.encode() in previous:
        def without_clock(value):
            return b'\n'.join(line for line in value.splitlines() if not line.startswith(b'Generated '))
        if without_clock(previous) == without_clock(output):
            return archive
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        temp.write(output)
        staged = pathlib.Path(temp.name)
    staged.replace(path)
    return archive
