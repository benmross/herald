"""Append-only, durable application checkpoints with field and attachment evidence.

No cookies, passwords, SSNs or OTPs belong in this store. Protected controls are
redacted in the browser before scan results cross the protocol connection.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import tempfile
from urllib.parse import urlsplit, urlunsplit

from . import config, db

PROTECTED = re.compile(r'password|passcode|secret|token|captcha|verif(?:ication|y).?code|security.?code|auth(?:entication)?.?code|ssn|social.?security|tax.?id|birth|one.?time|\botp\b|last.?four|last.?4|national.?id|routing|credit.?card|bank.?account', re.I)
PROTECTED_JS = r'/password|passcode|secret|token|captcha|verif(?:ication|y).?code|security.?code|auth(?:entication)?.?code|ssn|social.?security|tax.?id|birth|one.?time|\botp\b|last.?four|last.?4|national.?id|routing|credit.?card|bank.?account/i'


def safe_url(url):
    u = urlsplit(url)
    return urlunsplit((u.scheme, u.netloc, u.path, '', ''))


def normalize(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip().casefold()


def identity(field):
    return json.dumps([field.get('frame_path', ''), field.get('id') or field.get('field_id') or field.get('name') or field.get('label'),
                       0 if field.get('field_id') else field.get('occurrence', 0), field.get('section', ''), field.get('label')], ensure_ascii=False)


def protected(field):
    return field.get('protected') or field.get('kind') == 'password' or bool(PROTECTED.search(' '.join(str(field.get(k, '')) for k in ('label','id','field_id','name'))))


def safe_field(field):
    if protected(field):
        return {'kind': 'protected', 'completed_manually': bool(field.get('value')), 'protected': True}
    out = {k:v for k,v in field.items() if k not in ('frame','hx') and k != 'options'}
    if 'options' in field:
        out['options'] = [{k:v for k,v in o.items() if k != 'hx'} if isinstance(o,dict) else o for o in (field['options'] or [])]
    out['key'] = identity(field)
    return out


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix='.checkpoint-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd,'w') as f:
            json.dump(payload,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
        d=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(d)
        finally:os.close(d)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def save(app_id, workspace, generation, stage, payload, *, verified=False):
    """Commit both an append-only DB row and a versioned atomic recovery file."""
    if any(protected(f) and 'value' in f for f in payload.get('fields', [])):
        raise ValueError('protected field values cannot be checkpointed')
    blob=json.dumps(payload,ensure_ascii=False)
    with db.session() as c:
        c.execute('PRAGMA synchronous=FULL')
        cid=c.execute('INSERT INTO application_checkpoints(app_id,ts,workspace,generation,stage,payload,verified) VALUES (?,?,?,?,?,?,?) RETURNING id',
                      (app_id,db.now(),workspace,generation,stage,blob,int(verified))).fetchone()[0]
    envelope={'id':cid,'app_id':app_id,'workspace':workspace,'generation':generation,'stage':stage,'verified':bool(verified),'payload':payload}
    atomic_json(config.HOME/'browser'/'checkpoints'/str(app_id)/(str(cid)+'.json'),envelope)
    return cid


def latest(app_id):
    with db.session() as c:
        r=c.execute('SELECT * FROM application_checkpoints WHERE app_id=? ORDER BY id DESC LIMIT 1',(app_id,)).fetchone()
        return {**dict(r),'payload':json.loads(r['payload'])} if r else None


def artifact(path):
    path=pathlib.Path(path).expanduser().resolve()
    data=path.read_bytes();digest=hashlib.sha256(data).hexdigest()
    dest=config.HOME/'browser'/'checkpoints'/'artifacts'/digest/path.name
    if not dest.exists():
        dest.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(dest,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
    return {'name':path.name,'sha256':digest,'size':len(data),'path':str(dest)}


def artifact_path(info):
    # A checkpoint is data, not permission to upload an arbitrary machine file.
    root=(config.HOME/'browser'/'checkpoints'/'artifacts').resolve()
    p=pathlib.Path(info['path']).resolve()
    if not p.is_relative_to(root) or not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=info['sha256']:
        raise ValueError('checkpoint attachment is unavailable or changed')
    return str(p)


def read_value(field):
    if field.get('kind') in ('radio','checks','buttonradio'):
        if 'selected_labels' in field:
            return field['selected_labels']
        return [o['label'] for o in field.get('options',[]) if o.get('checked')]
    return field.get('selected_label',field.get('value',''))


def matches(field, want):
    from . import apply, ats
    if want is None or protected(field):return True
    actual=read_value(field);kind=field['kind']
    if re.search(r'location|\bcity\b',field.get('label',''),re.I) and kind in ('text','combobox'):
        # Location boxes autocomplete: "College Park, Maryland" is kept as
        # "College Park, Maryland, United States" (Ashby, 27 Oct 2026).
        want_words=set(re.findall(r'[a-z]+',normalize(ats.plain(want))))
        return bool(want_words) and want_words<=set(re.findall(r'[a-z]+',normalize(actual)))
    if kind in ('select','dropdown','combobox','prompt'):
        return bool(actual) and apply.best_option(want,[str(actual)]) is not None
    if kind in ('radio','buttonradio'):
        return bool(actual) and apply.best_option(want,actual) is not None
    if kind=='checks':
        if isinstance(want,bool) and len(field.get('options',[]))==1:return bool(actual)==want
        return all(apply.best_option(w,actual) is not None for w in (want if isinstance(want,list) else [want]))
    if kind=='check':return bool(actual)==bool(want)
    if kind=='date':return [int(x) for x in re.findall(r'\d+',str(actual))]==[int(x) for x in re.findall(r'\d+',str(want))]
    if kind=='file':return bool(actual)
    if re.search(r'phone|mobile',field.get('label',''),re.I):
        return re.sub(r'\D','',str(actual)).lstrip('1')==re.sub(r'\D','',str(want)).lstrip('1')
    return normalize(actual)==normalize(ats.plain(want))


class Recorder:
    def __init__(self, app_id, workspace, page, adapter):
        from . import browser_workspaces as bw
        self.app_id,self.workspace,self.page,self.adapter=app_id,workspace,page,adapter
        self.generation=bw.get(workspace)['generation']
        old=latest(app_id)
        self.writes=dict(old['payload'].get('writes',{})) if old else {}
        if old and not getattr(adapter,'carries_writes',False):
            # A single-page form is written in full on every run, so a write
            # from an earlier run only lingers to fail review against the new
            # answers (Amplify, 27 Oct 2026). Multi-page adapters set
            # carries_writes: their earlier pages are not visited again.
            self.writes={}
        self.artifacts=list(old['payload'].get('artifacts',[])) if old else []
        self.errors=[]
        self.history=dict(old['payload'].get('field_history',{})) if old else {}
        self.old_fields=list(self.history.values()) or (old['payload'].get('fields',[]) if old else [])
        self.recovering=bool(old and (old['workspace']!=workspace or old['generation']!=self.generation))

    def check_control(self):
        from . import browser_workspaces as bw
        bw.assert_agent(self.workspace,self.page.url)
        bw.admit(self.workspace)
        if bw.get(self.workspace)['generation']!=self.generation:
            raise RuntimeError('browser generation changed; recovery required')

    def snapshot(self, stage, *, verified=False, result=None):
        fields=[safe_field(f) for f in self.adapter.scan()]
        for f in fields:
            if f.get('key'):self.history[f['key']]=f
        payload={'url':safe_url(self.page.url),'fields':fields,'field_history':self.history,'writes':self.writes,'artifacts':self.artifacts,'errors':self.errors}
        if result is not None:
            payload['result']={k:result.get(k) for k in ('done','step','pending','failed')}
        return save(self.app_id,self.workspace,self.generation,stage,payload,verified=verified)

    def upload(self, path, field=None):
        info=artifact(path)
        if field:
            info.update(field_key=identity(field),field_label=field.get('label'),required=bool(field.get('required')))
        if not any(x['sha256']==info['sha256'] for x in self.artifacts):self.artifacts.append(info)
        self.snapshot('before-upload')
        return info

    def restore_uploads(self):
        if not self.recovering:
            return
        for info in list(self.artifacts):
            f=next((f for f in self.adapter.scan() if f['kind']=='file' and (identity(f)==info.get('field_key') or f.get('label')==info.get('field_label'))),None)
            if f and not f.get('value'):
                self.adapter.set(f,artifact_path(info))

    def before(self, field, value):
        self.check_control()
        if not protected(field):
            self.writes[identity(field)]={'field':safe_field(field),'intended':value,'verified':False}
        self.snapshot('before-field')

    def after(self, field, value, ok):
        if protected(field):return ok
        key=identity(field)
        fresh=next((f for f in self.adapter.scan() if identity(f)==key),None)
        verified=bool(ok and fresh is not None and matches(fresh,value))
        # File inputs may clear after upload; attachment evidence is checked at review.
        if field['kind']=='file':verified=bool(ok)
        self.writes[key]={'field':safe_field(field),'intended':value,'observed':read_value(fresh) if fresh else None,'verified':verified}
        if not verified and ok:self.errors.append('Readback failed: '+field.get('label','field'))
        self.snapshot('after-field')
        return verified

    def saved_value(self, field):
        """Replay an exact structured choice only for the same field identity."""
        if not self.recovering or protected(field) or field.get('kind')=='file':return None,False
        key=identity(field)
        written=self.writes.get(key)
        f=next((x for x in self.old_fields if x.get('key')==key),None)
        if f and f.get('kind')!='file' and read_value(f) not in (None,''):
            actual=read_value(f)
        elif written and written.get('verified'):
            actual=written.get('observed')
        else:
            return None,False
        if field['kind'] in ('radio','buttonradio','select','dropdown','combobox','prompt'):
            if not actual:return None,False
            if isinstance(actual,list):actual=actual[0]
            return '^'+re.escape(str(actual))+'$',True
        if field['kind']=='checks':return ['^'+re.escape(str(x))+'$' for x in actual],True
        return actual,actual not in (None,'')

    def verify_review(self, result):
        from . import ats
        failures=[]
        if not result.get('done') or result.get('pending') or result.get('failed'):
            return ['Application has not reached a clean review page.']
        fields=self.adapter.scan()
        # File inputs are left out: Ashby empties a required file input after
        # the upload it shows (27 Oct 2026). Attachments are checked below.
        if self.page.locator('input:not([type=file]):invalid:visible,select:invalid:visible,textarea:invalid:visible,[aria-invalid=true]:not([type=file]):visible').count():
            failures.append('The review page contains invalid fields.')
        for f in fields:
            if protected(f):continue
            if f.get('required') and f['kind']!='file' and not read_value(f):failures.append('Required field remains empty: '+f['label'])
            written=self.writes.get(identity(f))
            if written and not matches(f,written['intended']):failures.append('Field changed after it was verified: '+f['label'])
        bad=[v for v in self.writes.values() if not v.get('verified')]
        if bad:failures.append('Some field writes have not passed readback verification.')
        # Only boolean attachment evidence leaves the page; never save its body text.
        if self.adapter.resume and not self.artifacts:
            # Uploaded by an earlier run that kept no record (Workday keeps the
            # file server-side between visits). The page naming the current
            # resume file is evidence enough: it is recorded now, and the
            # name check below still has to pass.
            self.artifacts.append(artifact(self.adapter.resume))
        names=[x['name'] for x in self.artifacts]
        evidence=[False for _ in names]
        for frame in self.page.frames:
            try:
                found=frame.evaluate('''names=>{const text=document.body.innerText;const files=[...document.querySelectorAll('input[type=file]')].flatMap(e=>[...e.files].map(f=>f.name));return names.map(n=>text.includes(n)||files.includes(n))}''',names)
                evidence=[a or b for a,b in zip(evidence,found)]
            except Exception:
                continue
        for n,found in zip(names,evidence):
            if not found:failures.append('Attachment must be restored or verified: '+n)
        if isinstance(self.adapter,ats.Workday):
            expected=self.adapter.bank.get('experience',[])
            # Compare the final review, not only the fields previously written.
            checks=self.page.evaluate(r'''items=>{let t=document.body.innerText.toLowerCase();let norm=s=>s.toLowerCase().replace(/[^a-z0-9]/g,'');let nt=norm(t);let dates=(t.match(/\b\d{1,2}\/\d{4}\b/g)||[]).map(s=>s.split('/').map(Number).join('/'));return items.map(x=>({company:nt.includes(norm(x.company)),title:nt.includes(norm(x.title)),start:dates.includes(x.start.split('/').map(Number).join('/')),end:dates.includes(x.end.split('/').map(Number).join('/'))}))}''',expected)
            for i,ch in enumerate(checks):
                if not all(ch.values()):failures.append('Review work history differs from the answer bank: entry '+str(i+1))
            edu_count=self.page.evaluate(r"()=>(document.body.innerText.match(/\bEducation\s+\d+\b/g)||[]).length")
            if edu_count!=len(self.adapter.bank.get('education',[])):failures.append('Review education count differs from the answer bank.')
        self.snapshot('review',verified=not failures,result={**result,'failed':failures})
        return failures
