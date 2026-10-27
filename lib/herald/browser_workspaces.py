"""Durable browser ownership. Separate displays let human review and filling coexist.

The OS lock allows one Playwright client per worker. Human ownership is durable,
so an expired link or a dead agent never makes review tabs available to refills.
The controller does not own the worker processes' lifetimes.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import socket
import subprocess
import time
import uuid
from urllib.parse import urlsplit

from . import config, db


class Busy(RuntimeError):
    pass


class Capacity(Busy):
    pass


def name(value=None):
    value = value or os.environ.get('HERALD_BROWSER_WORKSPACE', 'default')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,39}', value):
        raise ValueError('invalid browser workspace name')
    return value


def get(workspace='default'):
    workspace = name(workspace)
    with db.session() as c:
        if workspace == 'default':
            c.execute("INSERT OR IGNORE INTO browser_workspaces(id,slot,state,created_at) VALUES ('default',0,'agent',?)", (db.now(),))
        r = c.execute('SELECT * FROM browser_workspaces WHERE id=?', (workspace,)).fetchone()
        return dict(r) if r else None


def all_workspaces():
    with db.session() as c:
        return [dict(r) for r in c.execute('SELECT * FROM browser_workspaces ORDER BY slot')]


def setting(key, default):
    return config.get('browser.' + key, default)


def ports(workspace='default'):
    r = get(workspace)
    if not r:
        raise LookupError('unknown browser workspace')
    slot = r['slot']
    return {'cdp': int(setting('cdp_port', 9333)) + slot,
            'vnc': int(setting('vnc_port', 5993)) + slot,
            'display': int(setting('display', 93)) + slot}


def account(url):
    u = urlsplit(url)
    # Workday tenants share an account across locale and requisition paths.
    return u.hostname or ''


def available_memory_mb():
    try:
        for line in open('/proc/meminfo'):
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) // 1024
    except OSError:
        return None


def admit(workspace=None):
    free = available_memory_mb()
    if free is not None and free < int(setting('minimum_available_mb', 2048)):
        raise Capacity('Browser work paused to preserve memory and existing drafts; finish a review group before opening more work.')
    if workspace and workspace != 'default':
        # Use the worker's cgroup, not only machine-wide free memory. The old
        # fixed cap killed Chromium despite available RAM elsewhere.
        r=subprocess.run(['systemctl','--user','show','herald-browser-worker@'+name(workspace)+'.service','-p','MemoryCurrent','-p','MemoryMax'],capture_output=True,text=True,timeout=3)
        values=dict(line.split('=',1) for line in r.stdout.splitlines() if '=' in line)
        current,limit=values.get('MemoryCurrent','0'),values.get('MemoryMax','infinity')
        if current.isdigit() and limit.isdigit() and int(limit)>0 and int(current)>int(limit)*0.85:
            raise Capacity('This browser worker is near its memory limit. Its checkpoint is preserved; no further fields will be changed.')


def create(workspace=None):
    admit()
    with db.session() as c:
        c.execute('BEGIN IMMEDIATE')
        live = c.execute("SELECT * FROM browser_workspaces WHERE slot>0 AND state!='closed'").fetchall()
        if len(live) >= int(setting('max_workspaces', 2)):
            raise Capacity('All browser workspaces are occupied. Existing review tabs are protected; no new forms were opened.')
        slot = c.execute('SELECT COALESCE(MAX(slot),0)+1 FROM browser_workspaces').fetchone()[0]
        workspace = name(workspace or 'batch-' + uuid.uuid4().hex[:12])
        c.execute("INSERT INTO browser_workspaces(id,slot,state,created_at) VALUES (?,?,'agent',?)", (workspace, slot, db.now()))
    return get(workspace)


def binding(app_id):
    with db.session() as c:
        r = c.execute('SELECT * FROM application_browser WHERE app_id=?', (app_id,)).fetchone()
        return dict(r) if r else None


def bind(app_id, workspace, tab, generation=None):
    r = get(workspace)
    with db.session() as c:
        c.execute('INSERT OR REPLACE INTO application_browser(app_id,workspace,tab,generation) VALUES (?,?,?,?)',
                  (app_id, workspace, tab, r['generation'] if generation is None else generation))


def choose(app_id, url):
    old = binding(app_id)
    if old and old['workspace'] != 'default':
        assert_agent(old['workspace'], url)
        return old['workspace']
    for r in all_workspaces():
        if r['slot'] == 0 or r['state'] != 'agent':
            continue
        with db.session() as c:
            count = c.execute("SELECT count(*) FROM application_browser b JOIN applications a ON a.id=b.app_id WHERE b.workspace=? AND a.status NOT IN ('submitted','rejected','closed','withdrawn')", (r['id'],)).fetchone()[0]
        if count < int(setting('batch_size', 10)):
            assert_agent(r['id'], url)
            return r['id']
    r = create()
    assert_agent(r['id'], url)
    return r['id']


def assert_agent(workspace, url=None):
    r = get(workspace)
    if not r or r['state'] != 'agent':
        raise Busy('This browser workspace is reserved for your review. Automation cannot change it.')
    if url:
        host = account(url)
        with db.session() as c:
            urls = c.execute("SELECT a.url FROM applications a JOIN application_browser b ON b.app_id=a.id JOIN browser_workspaces w ON w.id=b.workspace WHERE w.state IN ('human','review') AND w.id!=?", (workspace,)).fetchall()
        if any(account(x['url']) == host for x in urls):
            raise Busy('This employer account is in a review workspace; other applications on that account are paused.')


@contextlib.contextmanager
def lease(workspace='default', *, human=False, timeout=8):
    workspace = name(workspace)
    if not human:
        assert_agent(workspace)
    folder = config.HOME / 'run' / 'browser-locks'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(folder / (workspace + '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Busy('Another session owns this browser workspace; retry after it checkpoints.')
                time.sleep(0.1)
        if not human:
            assert_agent(workspace)
        elif get(workspace)['state'] not in ('human', 'review'):
            raise Busy('Human control has not been granted for this workspace.')
        yield
    finally:
        os.close(fd)  # flock is released even after a killed process.


def take_control(workspace, *, review=False):
    r = get(workspace)
    if not r or r['state'] == 'closed':
        raise Busy('Browser workspace is unavailable.')
    with db.session() as c:
        c.execute('UPDATE browser_workspaces SET state=?,human_until=NULL WHERE id=?', ('review' if review else 'human', workspace))


def release_control(workspace):
    # Review ownership survives Done and link expiry until explicitly returned.
    with db.session() as c:
        c.execute("UPDATE browser_workspaces SET state='agent',human_until=NULL WHERE id=? AND state='human'", (workspace,))


def note_browser(workspace, browser_id):
    with db.session() as c:
        r = c.execute('SELECT * FROM browser_workspaces WHERE id=?', (workspace,)).fetchone()
        if not r:
            raise LookupError(workspace)
        changed = r['browser_id'] is not None and r['browser_id'] != browser_id
        if changed:
            c.execute('UPDATE browser_workspaces SET generation=generation+1,last_error=? WHERE id=?', ('Browser restarted; recovery and verification required.', workspace))
            c.execute("UPDATE application_review_groups SET state='recovery' WHERE workspace=? AND state='review'", (workspace,))
        c.execute('UPDATE browser_workspaces SET browser_id=? WHERE id=?', (browser_id, workspace))
    return changed


def start(workspace):
    if workspace == 'default':
        return
    admit()
    r = subprocess.run(['systemctl', '--user', 'start', 'herald-browser-worker@' + name(workspace) + '.service'], capture_output=True, text=True, timeout=20)
    if r.returncode:
        raise RuntimeError('Browser worker could not start; install the user service templates first.')


def ready_group(workspace, *, partial=False, app_ids=None):
    # A different session may still be filling this workspace. Never take a
    # partly active desktop away or publish a group behind its writer's back.
    if not get(workspace) or get(workspace)['state'] != 'agent':
        return None
    try:
        with lease(workspace,timeout=0):
            return _ready_group(workspace,partial=partial,app_ids=app_ids)
    except Busy:
        return None


def _ready_group(workspace, *, partial=False, app_ids=None):
    """Atomically reserve only verified current-generation applications for review."""
    with db.session() as c:
        c.execute('BEGIN IMMEDIATE')
        w = c.execute('SELECT * FROM browser_workspaces WHERE id=?', (workspace,)).fetchone()
        if not w or w['state'] != 'agent':
            return None
        rows = c.execute("""SELECT a.id FROM applications a JOIN application_browser b ON b.app_id=a.id
          WHERE b.workspace=? AND a.status='filled' AND b.generation=?
          AND EXISTS(SELECT 1 FROM application_checkpoints p WHERE p.app_id=a.id
            AND p.id=(SELECT max(id) FROM application_checkpoints WHERE app_id=a.id)
            AND p.verified=1 AND p.generation=? AND p.workspace=?) ORDER BY a.id""",
                         (workspace, w['generation'], w['generation'], workspace)).fetchall()
        size = int(setting('batch_size', 10))
        if app_ids is not None:
            rows = [r for r in rows if r['id'] in app_ids]
            if len(rows) != len(app_ids):
                raise Busy('Some requested applications do not have current verified checkpoints; no group was created.')
        if not rows or (len(rows) < size and not partial):
            return None
        group = 'review-' + uuid.uuid4().hex[:12]
        c.execute('INSERT INTO application_review_groups(id,workspace,created_at) VALUES (?,?,?)', (group, workspace, db.now()))
        c.executemany('INSERT INTO application_review_members(group_id,app_id) VALUES (?,?)', [(group, r['id']) for r in rows[:size]])
        c.execute("UPDATE browser_workspaces SET state='review' WHERE id=?", (workspace,))
    return group


def group_info(group):
    with db.session() as c:
        r = c.execute('SELECT * FROM application_review_groups WHERE id=?', (group,)).fetchone()
        if not r:
            raise LookupError('unknown review group')
        members = c.execute('''SELECT a.id,a.company,a.title,a.status,b.tab,b.generation
          FROM application_review_members m JOIN applications a ON a.id=m.app_id
          JOIN application_browser b ON b.app_id=a.id WHERE m.group_id=? ORDER BY a.id''', (group,)).fetchall()
        return {**dict(r), 'members': [dict(m) for m in members]}


def mark_submitted(group, app_id):
    from . import apply
    with db.session() as c:
        if not c.execute('SELECT 1 FROM application_review_members WHERE group_id=? AND app_id=?', (group, app_id)).fetchone():
            raise ValueError('application is not in this review group')
        apply.set_state(c, app_id, status='submitted', note='You marked this application submitted in the review group.')
        db.record_action(c, actor='user', tier='green', kind='application.review', target=str(app_id), summary='You marked an application as already submitted; Herald did not submit it.')


def finish_group(group):
    info = group_info(group)
    if any(m['status'] not in ('submitted', 'rejected', 'closed', 'withdrawn') for m in info['members']):
        raise Busy('Some applications are still unsubmitted. Their tabs remain protected.')
    with db.session() as c:
        c.execute("UPDATE application_review_groups SET state='finished' WHERE id=?", (group,))
        # Other unfinished applications in the workspace still prevent disposal.
        remaining = c.execute("SELECT count(*) FROM application_browser b JOIN applications a ON a.id=b.app_id WHERE b.workspace=? AND a.status NOT IN ('submitted','rejected','closed','withdrawn')", (info['workspace'],)).fetchone()[0]
        c.execute('UPDATE browser_workspaces SET state=? WHERE id=?', ('agent' if remaining else 'closed', info['workspace']))
    return info['workspace']


def return_group(group):
    info=group_info(group)
    with db.session() as c:
        c.execute("UPDATE application_review_groups SET state='returned' WHERE id=?",(group,))
        c.execute("UPDATE browser_workspaces SET state='agent' WHERE id=?",(info['workspace'],))
        c.execute("UPDATE applications SET status='filling' WHERE id IN (SELECT app_id FROM application_review_members WHERE group_id=?) AND status='filled'",(group,))


def stop_closed(workspace):
    if workspace == 'default' or get(workspace)['state'] != 'closed':
        raise Busy('Unfinished browser work cannot be stopped.')
    subprocess.run(['systemctl','--user','stop','herald-browser-worker@'+name(workspace)+'.service'],check=True,timeout=20)


def publish(group, minutes=240):
    from . import browse, notify
    info=group_info(group)
    if info['state'] != 'review':
        raise Busy('This group requires recovery or has already been finished.')
    if not browse.view_base():
        raise RuntimeError('Set the private browser view URL before publishing review groups.')
    token=browse.new_view(info['members'][0]['tab'],'Review and submit your prepared applications',minutes,workspace=info['workspace'],group=group)
    url=browse.view_link(token)
    sent=notify.telegram(f"Your next {len(info['members'])} applications are ready for review. Switch between them here:\n{url}\n\nThis group is reserved for you while other groups are prepared. Submit on each employer’s page, then use “I submitted this” to record it.",thread_id=notify._live_topic_thread())
    return sent


@contextlib.contextmanager
def account_lease(url, timeout=8):
    import hashlib
    key=hashlib.sha256(account(url).encode()).hexdigest()
    folder=config.HOME/'run'/'browser-locks'
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(folder/('account-'+key+'.lock'),os.O_CREAT|os.O_RDWR,0o600)
    deadline=time.monotonic()+timeout
    try:
        while True:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise Busy('Another workspace is using this employer account; its current operation is protected.')
                time.sleep(0.1)
        yield
    finally:os.close(fd)
