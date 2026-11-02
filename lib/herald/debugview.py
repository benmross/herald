"""Explain provider records as activities without discarding the original log.

Time between tools includes model generation, transport and unobserved work.
Only timestamped call/result pairs establish observed tool waits. Interval
unions keep parallel calls from being counted twice; inter-turn idle is omitted.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime


def milliseconds(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp() * 1000
    except (ValueError, TypeError):
        return None


def unpack(event):
    obj = event['raw']
    if obj.get('channel'):
        obj = obj.get('data') or {}
        if isinstance(obj, str):
            try:
                obj = json.loads(obj)
            except ValueError:
                obj = {'text': obj}
    if not isinstance(obj, dict):
        obj = {}
    payload = obj.get('payload') or obj
    if not isinstance(payload, dict):
        payload = obj
    return obj, payload, payload.get('type') or obj.get('type') or obj.get('method') or event['name']


def body(content):
    if isinstance(content, str):
        return content
    return '\n'.join(c.get('text') or c.get('thinking') or c.get('content') or ''
                     for c in content or [] if isinstance(c, dict) and
                     isinstance(c.get('text') or c.get('thinking') or c.get('content') or '', str))


def pretty(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)


def tool_name(name):
    return {'functions.exec': 'Run tool script', 'exec': 'Run tool script',
            'wait': 'Wait for background work', 'view_image': 'Inspect image',
            'sleep': 'Wait', 'exec_command': 'Run command',
            'functions.exec_command': 'Run command', 'Bash': 'Run command',
            'apply_patch': 'Edit files', 'functions.apply_patch': 'Edit files',
            'Read': 'Read file', 'Write': 'Write file', 'Edit': 'Edit file',
            'functions.wait': 'Wait for background work', 'write_stdin': 'Check running command',
            'functions.write_stdin': 'Check running command'}.get(name, name or 'Tool call')


def breakdown(turns, activities):
    output = []
    for turn in turns:
        start, end = turn.get('start_ms'), turn.get('end_ms')
        if start is None or end is None or end < start:
            continue
        edges = {start: [], end: []}
        for a in activities:
            if a['turn'] != turn['id'] or a['kind'] != 'tool' or a.get('start_ms') is None:
                continue
            left, right = max(start, a['start_ms']), min(end, a.get('end_ms') if a.get('end_ms') is not None else end)
            if right <= left:
                continue
            confirmed = a.get('end_ms') is not None
            edges.setdefault(left, []).append((True, a['seq'], confirmed))
            edges.setdefault(right, []).append((False, a['seq'], confirmed))
        active, segments = {}, []
        for i, moment in enumerate(sorted(edges)):
            if i:
                kind = 'tool' if any(active.values()) else 'unconfirmed' if active else 'between'
                if moment > previous:
                    segments.append({'start_ms': previous, 'end_ms': moment, 'ms': moment-previous,
                                     'kind': kind, 'seqs': list(active)})
            for add, seq, confirmed in edges[moment]:
                if add:
                    active[seq] = confirmed
                else:
                    active.pop(seq, None)
            previous = moment
        totals = {k: sum(s['ms'] for s in segments if s['kind'] == k) for k in ('tool','between','unconfirmed')}
        output.append({**turn, 'duration_ms': end-start, 'tool_ms': totals['tool'],
                       'between_ms': totals['between'], 'unconfirmed_ms': totals['unconfirmed'],
                       'segments': segments})
    return {'turns': output, 'observed_ms': sum(t['duration_ms'] for t in output),
            'tool_ms': sum(t['tool_ms'] for t in output),
            'between_ms': sum(t['between_ms'] for t in output),
            'unconfirmed_ms': sum(t['unconfirmed_ms'] for t in output)}


def build(events):
    from .transcripts import clean_user
    activities, turns, points, pending, instructions = [], [], [], {}, {}
    current = None
    last_context = None
    measurement_index = 0
    technical = 0

    def add(event, kind, title, text='', explanation='', technical_only=False, **extra):
        a = {'seq': event['seq'], 'end_seq': event['seq'], 'ts': event['ts'], 'kind': kind,
             'name': title, 'text': text, 'explanation': explanation, 'technical': technical_only,
             'turn': current['id'] if current else None, 'records': [event['raw']],
             'start_ms': milliseconds(event['ts']), 'end_ms': None, **extra}
        activities.append(a)
        return a

    def start_turn(event, identity=None, explicit=False):
        nonlocal current
        current = {'id': identity or 'turn-'+str(event['seq']), 'number': len(turns)+1,
                   'start_ms': milliseconds(event['ts']), 'end_ms': milliseconds(event['ts']),
                   'ts': event['ts'], 'complete': False, 'explicit': explicit}
        turns.append(current)
        return current

    def instruction(event, title, text, explanation):
        digest = hashlib.sha256(text.encode()).hexdigest()
        if digest in instructions:
            first = instructions[digest]
            first['repeats'] = first.get('repeats', 0)+1
            add(event, 'context', 'Unchanged instructions supplied again', '',
                'Same content as event #'+str(first['seq'])+'. This record alone does not prove that a second copy was added to the model context.',
                True, repeat_of=first['seq'])
        else:
            a = add(event, 'context', title, text, explanation)
            a['characters'] = len(text)
            instructions[digest] = a

    def call(event, call_id, name, given):
        a = add(event, 'tool', tool_name(name), pretty(given),
                'Observed wait runs from the tool request to its recorded result; it includes tool transport and scheduling.',
                call_id=call_id, tool=name, input=pretty(given), output=None, children=[])
        pending[call_id or 'seq-'+str(event['seq'])] = a
        return a

    def result(event, call_id, value):
        a = pending.pop(call_id, None)
        if a is None:
            a = add(event, 'tool', 'Tool result (request missing)', '',
                    'The log has a result but no matching request, so its wait duration is unknown.',
                    call_id=call_id, input=None, output=None, children=[])
            a['start_ms'] = None
        a.update(output=pretty(value), end_ms=milliseconds(event['ts']), end_ts=event['ts'], end_seq=event['seq'])
        a['records'].append(event['raw']) if a['seq'] != event['seq'] else None
        if a['start_ms'] is not None and a['end_ms'] is not None:
            a['duration_ms'] = max(0, a['end_ms']-a['start_ms'])
        return a

    for event in events:
        obj, p, typ = unpack(event)
        method, params = obj.get('method') or '', obj.get('params') or {}
        when = milliseconds(event['ts'])
        if typ == 'task_started' or method == 'turn/started' or typ == 'turn.started':
            identity = p.get('turn_id') or (params.get('turn') or {}).get('id')
            start_turn(event, identity, True)
            add(event, 'boundary', 'Turn started', '', 'The engine began answering a message. A session can contain many turns; this is not a shell task.')
        elif typ == 'task_complete' or method == 'turn/completed' or typ in ('turn.completed','turn.failed'):
            if current:
                current['complete'] = True
                current['end_ms'] = when or current['end_ms']
            add(event, 'boundary', 'Turn finished', '', 'The engine reported the end of this turn. Time waiting for your next message is excluded from the time breakdown.')
        elif typ == 'session_meta':
            base = p.get('base_instructions') or {}
            text = base.get('text','') if isinstance(base,dict) else pretty(base)
            instruction(event, 'Session setup and base instructions', text,
                        'Saved provider setup: working directory, engine version and base instructions. It describes the session; it is not another model call. Full metadata is in the original record.')
        elif typ in ('user','assistant','message'):
            message = obj.get('message') or p
            role = message.get('role') or typ
            content = message.get('content') or []
            text = body(content)
            blocks = content if isinstance(content,list) else []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'tool_use':
                    call(event,block.get('id'),block.get('name'),block.get('input'))
                elif block.get('type') == 'tool_result':
                    result(event,block.get('tool_use_id'),block.get('content',block))
            if role in ('developer','system') or (role == 'user' and text.lstrip().startswith(('# AGENTS.md instructions','<user_instructions>','Base directory for this skill:'))):
                instruction(event, 'Instructions supplied to the engine', text,
                            'System, developer or project instructions. Providers sometimes store project instructions under the user role; that does not mean you typed this message. Expand to inspect the content.')
            elif role == 'user' and text.strip():
                cleaned = clean_user(text)
                if cleaned:
                    if current is None or current['complete'] or not current['explicit']:
                        start_turn(event)
                    add(event,'user','Your message',cleaned,'The text you sent. The original record also contains any bridge wrapper, orientation and attached context.')
                else:
                    instruction(event,'Injected context',text,'Context supplied by Herald or the engine, rather than a new message from you.')
            elif role == 'assistant' and text.strip():
                add(event,'assistant','Assistant reply' if p.get('phase')=='final_answer' else 'Assistant message',text,
                    'Visible text from the assistant. This can be a progress update or the final answer.')
        elif typ in ('function_call','custom_tool_call'):
            call(event,p.get('call_id'),p.get('name'),p.get('arguments') or p.get('input') or '')
        elif typ in ('function_call_output','custom_tool_call_output'):
            result(event,p.get('call_id'),p.get('output') or '')
        elif typ == 'item_completed' or method in ('item/started','item/completed') or typ in ('item.started','item.completed'):
            item = p.get('item') or params.get('item') or {}
            itype = (item.get('type') or '').replace('_','').lower()
            if itype in ('commandexecution','filechange','mcptoolcall','websearch','imageview'):
                nested = next(reversed(pending.values()),None) if typ == 'item_completed' else None
                if nested:
                    nested['children'].append({'name':item.get('type'), 'ts':event['ts'],
                                               'text':pretty(item), 'seq':event['seq']})
                    nested['records'].append(event['raw'])
                else:
                    identity = item.get('id')
                    given = item.get('command') or item.get('arguments') or item
                    if method == 'item/started' or typ == 'item.started':
                        call(event,identity, 'Run command' if itype=='commandexecution' else item.get('type'),given)
                    elif identity in pending:
                        result(event,identity,item.get('aggregatedOutput') or item.get('aggregated_output') or item)
                    else:
                        a=add(event,'tool', 'Command executed' if itype=='commandexecution' else item.get('type'),pretty(item),
                              'Provider completion record. Its embedded duration can describe execution, but a zero-length receipt timestamp does not measure the wait.',
                              input=pretty(given),output=pretty(item.get('aggregated_output') or item.get('aggregatedOutput') or ''),children=[])
                        a['start_ms']=None
            elif itype in ('agentmessage','usermessage','reasoning') and typ == 'item_completed':
                technical += 1  # Mirrors canonical response_item content.
            elif itype == 'agentmessage' and method != 'item/started':
                text = item.get('text') or body(item.get('content'))
                if text:
                    add(event,'assistant','Assistant message',text)
            elif itype == 'usermessage' and method == 'item/completed':
                text = clean_user(body(item.get('content')))
                if text:
                    if current is None:
                        start_turn(event)
                    add(event,'user','Your message',text,
                        'The message accepted by the engine for this turn. The launch record separately captures its supplied context.')
            else:
                technical += 1
        elif typ == 'reasoning':
            text = body(p.get('summary'))
            if text:
                add(event,'reasoning','Reasoning summary',text,'A provider-visible summary, not the model\'s hidden reasoning.')
            else:
                technical += 1
        elif typ == 'turn_context':
            add(event,'context','Turn settings',pretty({k:p[k] for k in ('model','effort','cwd','approval_policy') if k in p}),
                'Settings saved for this turn, such as model, reasoning effort and permissions. Repeated settings are snapshots, not repeated commands.', True)
        elif obj.get('channel') == 'launch':
            instruction(event,'Herald launch inputs',pretty(p),
                        'Herald captured the prompt, appended instructions and instruction-file snapshots. This overlaps the provider transcript; it is not evidence of another injection.')
        elif event['kind'] == 'context' and event.get('context') is not None:
            technical += 1
        else:
            technical += 1
        if current and when is not None and not current['complete']:
            current['end_ms'] = max(current['end_ms'] or when, when)
        if event.get('context') is not None:
            nearby = next((a for a in reversed(activities) if not a['technical'] and a['kind'] in ('tool','assistant','user','reasoning')), None)
            recent = [a for a in activities[measurement_index:] if not a['technical']]
            value = event['context']
            points.append({'seq':event['seq'],'ts':event['ts'],'context':value,'window':event.get('window'),
                           'delta':value-last_context if last_context is not None else None,
                           'turn':current['id'] if current else None,'anchor_seq':nearby['seq'] if nearby else event['seq'],
                           'nearby':nearby['name'] if nearby else 'Session setup',
                           'activities':len(recent), 'tools':sum(a['kind']=='tool' for a in recent)})
            last_context, measurement_index = value, len(activities)
    for a in activities:
        if a['kind'] == 'tool':
            a['text'] = '\n'.join(v for v in (a.get('input'),a.get('output')) if v)
            if a.get('output') is None and a.get('input') is not None:
                a['explanation'] += ' No result was recorded in this source yet.'
    return {'activities':activities,'context':points,'timing':breakdown(turns,activities),
            'technical_records':technical,'raw_count':len(events)}
