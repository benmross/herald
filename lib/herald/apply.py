"""Filling job applications with as little model time as possible.

The first autonomous batch (23 Oct 2026: fourteen applications in about
three hours) spent most of its time and session on things a loop can do:
finding fields, picking dropdown options, re-answering the same question
asked five ways, and one-off scripts per site. This module keeps all of that
in Python, so a session is only needed for questions nobody has answered yet,
and it answers them in one batch.

Three pieces:

- **The answer bank**, a JSON file in the ledger (`apply.bank`). It holds the
  applicant's profile (name, school, links, resume path, work history) and a
  list of answers keyed by a regex over question labels. A question matched
  once is answered forever after without a model. Answers can be scoped to
  one company, which is how "Why do you want to work here?" stays per company.
- **The applications table** in facts.db: one row per application with its
  URL, system, status (filling, filled, submitted, rejected, closed), the
  questions still unanswered, and any per-application answers such as
  essays. `herald apply pending` lists every open question across every
  application in one compact block; `herald apply answer` stores the answers
  and refills. The `applications` collector moves rows to submitted or
  rejected from confirmation and rejection mail.
- **Adapters** (lib/herald/ats.py) that turn a page into fields and set
  them: Workday's multi-step wizard, and a generic adapter for Greenhouse,
  Ashby, Lever and SmartRecruiters, which reads the accessible form,
  shadow DOM included.

Submitting is never done here. Every adapter stops on the last page and
refuses its submit button; the person submits.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import secrets as _random
import string

from . import config, db

DONE = ("submitted", "rejected", "closed", "withdrawn")


# --- the answer bank -----------------------------------------------------------

def bank_path() -> pathlib.Path:
    return config.LEDGER / str(config.get("apply.bank", "documents/career/answers.json"))


def load_bank() -> dict:
    p = bank_path()
    if not p.exists():
        return {"profile": {}, "experience": [], "education": [], "answers": []}
    return json.loads(p.read_text())


def save_bank(bank: dict) -> None:
    p = bank_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(bank, indent=2, ensure_ascii=False) + "\n")


def norm(text: str) -> str:
    text = re.sub(r"[*]+", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text.rstrip(" ?:.")


def _scope_ok(scope, company: str | None) -> bool:
    if not scope:
        return True
    return bool(company) and norm(scope) in norm(company)


def lookup(bank: dict, label: str, company: str | None = None,
           app_answers: dict | None = None):
    """The stored answer for a question, or None. Returns (value, source).

    Order: this application's own answers, then bank answers scoped to this
    company, then unscoped bank answers. A value of null in the bank means
    "leave this blank on purpose", which is an answer too.
    """
    key = norm(label)
    if app_answers and key in app_answers:
        return app_answers[key], "application"
    scoped = [a for a in bank.get("answers", []) if a.get("scope") and _scope_ok(a["scope"], company)]
    general = [a for a in bank.get("answers", []) if not a.get("scope")]
    for a in scoped + general:
        try:
            if re.search(a["q"], label, re.I):
                v = a.get("a")
                if v == "@today":  # a signature date is always today's
                    v = dt.date.today().strftime("%m/%d/%Y")
                return v, "bank"
        except re.error:
            if norm(a["q"]) == key:
                return a.get("a"), "bank"
    return None, None


def has_answer(bank, label, company=None, app_answers=None) -> bool:
    key = norm(label)
    if app_answers and key in app_answers:
        return True
    for a in bank.get("answers", []):
        if a.get("scope") and not _scope_ok(a["scope"], company):
            continue
        try:
            if re.search(a["q"], label, re.I):
                return True
        except re.error:
            pass
    return False


def best_option(want, options: list[str]) -> str | None:
    """Pick the option an answer means. `want` is a regex or plain text."""
    if want is None or not options:
        return None
    want = str(want)
    exact = [o for o in options if norm(o) == norm(want)]
    if len(exact) == 1:
        return exact[0]
    try:
        full = [o for o in options if re.fullmatch(want, o, re.I)]
        if len(full) == 1:
            return full[0]
        # Only explicit patterns or an unambiguous descriptive phrase may
        # match part of an option. Never guess a degree/major by token overlap.
        if re.search(r'[\^$|.*+?\\\[()]', want) or len(want.split()) >= 4:
            hits = [o for o in options if re.search(want, o, re.I)]
            return hits[0] if len(hits) == 1 else None
    except re.error:
        pass
    return None


# --- the applications table ----------------------------------------------------

def record(con, *, url: str, company: str | None = None, title: str | None = None,
           ats: str | None = None, opportunity: int | None = None) -> int:
    row = con.execute("SELECT id FROM applications WHERE url = ?", (url,)).fetchone()
    if row:
        con.execute("""UPDATE applications SET company=COALESCE(?, company),
                       title=COALESCE(?, title), ats=COALESCE(?, ats),
                       opportunity_id=COALESCE(?, opportunity_id) WHERE id=?""",
                    (company, title, ats, opportunity, row["id"]))
        return row["id"]
    return con.execute("""INSERT INTO applications (url, company, title, ats, status,
                          opportunity_id, created_at) VALUES (?,?,?,?, 'filling', ?, ?)
                          RETURNING id""",
                       (url, company, title, ats, opportunity, db.now())).fetchone()[0]


def get(con, app_id: int):
    return con.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()


def app_answers(row) -> dict:
    return json.loads(row["answers"] or "{}") if row else {}


def set_state(con, app_id: int, *, status: str | None = None, pending=None,
              note: str | None = None) -> None:
    sets, args = [], []
    if status:
        sets.append("status = ?")
        args.append(status)
        if status == "filled":
            sets.append("filled_at = ?")
            args.append(db.now())
        if status == "submitted":
            sets.append("submitted_at = COALESCE(submitted_at, ?)")
            args.append(db.now())
    if pending is not None:
        sets.append("pending = ?")
        args.append(json.dumps(pending, ensure_ascii=False))
    if note:
        sets.append("notes = COALESCE(notes, '') || ?")
        args.append(f" [{dt.date.today()}] {note}")
    if sets:
        con.execute(f"UPDATE applications SET {', '.join(sets)} WHERE id = ?", (*args, app_id))
    row = get(con, app_id)
    if status and row and row["opportunity_id"]:
        opp = {"submitted": "applied", "rejected": "rejected", "closed": "expired"}.get(status)
        if opp:
            con.execute("UPDATE opportunities SET status = ? WHERE id = ?", (opp, row["opportunity_id"]))


def store_answer(con, *, label: str, value, app_id: int | None = None,
                 pattern: str | None = None, scope: str | None = None) -> str:
    """Save one answer: to the application (essays, one-offs) or to the bank."""
    if app_id is not None and not pattern and scope is None:
        row = get(con, app_id)
        ans = app_answers(row)
        ans[norm(label)] = value
        con.execute("UPDATE applications SET answers = ? WHERE id = ?",
                    (json.dumps(ans, ensure_ascii=False), app_id))
        return "application"
    bank = load_bank()
    q = pattern or "^" + re.escape(norm(label)).replace(r"\ ", r"\s+")
    bank.setdefault("answers", [])
    bank["answers"] = [a for a in bank["answers"] if not (a["q"] == q and a.get("scope") == scope)]
    bank["answers"].append({"q": q, "a": value, **({"scope": scope} if scope else {}),
                            "added": dt.date.today().isoformat()})
    save_bank(bank)
    return "bank"


# --- accounts ---------------------------------------------------------------------

def new_password() -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    while True:
        pw = "".join(_random.choice(alphabet) for _ in range(20))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in "!@#$%^&*-_=+" for c in pw)):
            return pw


def account_key(host: str) -> str:
    return "accounts." + re.sub(r"[^a-z0-9]+", "_", host.lower()).strip("_")


def account(host: str) -> dict | None:
    return config.secret(account_key(host))


def save_account(host: str, email: str, password: str, *, created: bool = True) -> None:
    """Herald's own copy, so it can sign back in. Never printed. `created` is
    False between storing the password and the site accepting it."""
    config.set_secret(account_key(host), {"email": email, "password": password,
                                          "created": created, "on": dt.date.today().isoformat()})


# --- verification mail -------------------------------------------------------------

CODE_RE = re.compile(r"\b(\d{6}|\d{8}|[A-Z0-9]{6})\b")
LINK_RE = re.compile(r"https://[^\s\"'<>]+(?:verif|activat|confirm)[^\s\"'<>]*", re.I)


def verification(since: dt.datetime, hint: str | None = None, wait_s: int = 120):
    """The newest verification code or link mailed since `since`, read live.

    Polls Gmail directly rather than waiting for the collector, because an
    account form times out long before the next collect. Returns
    {"code": ..., "link": ..., "subject": ...} or None.
    """
    import base64
    import time

    from . import google
    svc = google.service("gmail", "v1")
    q = f"after:{int(since.timestamp())} (verify OR verification OR confirm OR code OR activate)"
    if hint:
        q += f" {hint}"
    deadline = time.time() + wait_s
    while time.time() < deadline:
        msgs = svc.users().messages().list(userId="me", q=q, maxResults=5).execute().get("messages", [])
        for m in msgs:
            full = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
            subject = next((h["value"] for h in full["payload"].get("headers", [])
                            if h["name"].lower() == "subject"), "")
            texts = []

            def walk(part):
                data = part.get("body", {}).get("data")
                if data and part.get("mimeType", "").startswith("text/"):
                    texts.append(base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace"))
                for p in part.get("parts", []) or []:
                    walk(p)
            walk(full["payload"])
            body = "\n".join(texts)
            link = LINK_RE.search(body)
            plain = re.sub(r"<[^>]+>", " ", body)
            code = None
            for c in CODE_RE.findall(subject + " " + plain):
                if any(ch.isdigit() for ch in c):
                    code = c
                    break
            if link or code:
                return {"code": code, "link": link.group(0) if link else None, "subject": subject}
        time.sleep(8)
    return None


# --- running one application -------------------------------------------------------

def company_from_url(url: str) -> str | None:
    from urllib.parse import urlparse
    u = urlparse(url)
    parts = [x for x in u.path.split("/") if x]
    if "greenhouse" in u.netloc:
        if "for=" in u.query:
            return re.search(r"for=([^&]+)", u.query).group(1)
        return parts[0] if parts else None
    if any(h in u.netloc for h in ("ashbyhq", "lever.co", "smartrecruiters")):
        return parts[0] if parts else None
    if "myworkdayjobs" in u.netloc:
        return u.netloc.split(".")[0]
    return u.netloc.split(".")[-2] if "." in u.netloc else None


def resume_path(bank: dict) -> str:
    p = bank.get("profile", {}).get("resume")
    if not p:
        raise SystemExit("no resume: set profile.resume in the answer bank")
    path = pathlib.Path(p)
    return str(path if path.is_absolute() else config.LEDGER / path)


def canonical_key(url):
    from urllib.parse import urlsplit, parse_qs
    u = urlsplit(url)
    if 'myworkdayjobs.com' in u.netloc:
        m = re.search(r'_([A-Za-z0-9-]+)(?:/apply.*)?$', u.path)
        if m:
            return u.netloc.lower() + ':' + m.group(1)
    if 'icims.com' in u.netloc:
        m = re.search(r'/jobs/(\d+)', u.path)
        if m:
            return u.netloc.lower() + ':' + m.group(1)
    q = parse_qs(u.query)
    jid = q.get('gh_jid', q.get('jobId', []))
    return u.netloc.lower() + u.path.removesuffix('/application').rstrip('/') + (':' + jid[0] if jid else '')


def run(url: str, *, opportunity: int | None = None, company: str | None = None,
        title: str | None = None, workspace=None, retry=False) -> dict:
    """Open (or reuse) the application's tab, fill everything the bank answers,
    record what is left. Never submits."""
    from . import ats, browse, browser_workspaces as bw
    from .application_checkpoints import Recorder, latest, artifact_path
    import hashlib

    bank = load_bank()
    with db.session() as con:
        duplicate = next((r for r in con.execute('SELECT * FROM applications') if canonical_key(r['url']) == canonical_key(url)), None)
        if duplicate and duplicate['status'] in DONE:
            return {'app': duplicate['id'], 'tab': '', 'company': duplicate['company'], 'title': duplicate['title'] or '', 'ats': duplicate['ats'], 'done': False, 'filled': [], 'pending': [], 'failed': [], 'skipped': duplicate['status']}
        if duplicate:
            url = duplicate['url']
        if opportunity and not company:
            o = con.execute("SELECT org, title FROM opportunities WHERE id=?", (opportunity,)).fetchone()
            if o:
                company, title = company or o["org"], title or o["title"]
        known = con.execute("SELECT company, title FROM applications WHERE url=?", (url,)).fetchone()
        company = company or (known["company"] if known else None) or company_from_url(url)
        kind = ats.detect(url)
        app_id = record(con, url=url, company=company, title=title, ats=kind, opportunity=opportunity)
        answers = app_answers(get(con, app_id))
        fingerprint = hashlib.sha256(json.dumps([bank, answers, url], sort_keys=True).encode()).hexdigest()
        previous = con.execute('SELECT * FROM application_attempts WHERE app_id=?', (app_id,)).fetchone()
        if previous and previous['fingerprint'] == fingerprint and previous['failures'] >= int(config.get('apply.max_step_failures', 2)) and not retry:
            raise bw.Busy('Repeated failure is checkpointed. Complete the access step or fix the adapter, then use refill --retry; no credentials or forms were retried.')
    workspace = workspace or bw.choose(app_id, url)
    bw.assert_agent(workspace, url)
    with browse.Session(workspace) as s:
        s.claim_account(url)
        # Workday moves the tab to .../apply/applyManually, so match on the
        # posting's URL as a prefix; an exact match opened a second tab per run.
        base = url.split("#")[0].split("?")[0].rstrip("/")
        from urllib.parse import urlparse
        req = re.search(r"_([A-Za-z0-9-]+)$", base) if kind == "workday" else None
        page = next((pg for pg in s.pages() if pg.url.split("?")[0].startswith(base)
                     or (req and urlparse(pg.url).netloc == urlparse(url).netloc
                         and req.group(0) in pg.url)), None)
        if page is None:
            bw.admit()
            if len([p for p in s.pages() if p.url != 'about:blank']) >= int(config.get('browser.batch_size', 10)):
                raise bw.Capacity('This workspace has reached its tab limit; preserve or hand off this batch before opening more.')
            page = s.ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            browse.settle(page, 4000)
        # Fail fast on a field that went stale rather than waiting 30 s each.
        page.set_default_timeout(8000)
        page.bring_to_front()
        if kind == "generic":
            kind = ats.detect(page.url, page.content()[:20000])
        adapter = ats.adapter_for(kind)(page, bank, company, answers, resume=resume_path(bank))
        adapter.application_url = url
        bound = bw.get(workspace)
        bw.bind(app_id, workspace, s.tab_id(page))
        checkpoint = Recorder(app_id, workspace, page, adapter)
        adapter.checkpoint = checkpoint
        if checkpoint.recovering:
            resume = next((x for x in checkpoint.artifacts if x['name'] == pathlib.Path(adapter.resume).name), None)
            if resume:
                adapter.resume = artifact_path(resume)
        prior_checkpoint = latest(app_id)
        already_ready = bool(duplicate and duplicate['status']=='filled' and prior_checkpoint and prior_checkpoint['verified'] and not checkpoint.recovering and not retry)
        checkpoint.snapshot('opened')
        try:
            result = ({'done':True,'filled':[],'pending':[],'failed':[]} if already_ready else adapter.fill())
            issues = checkpoint.verify_review(result) if result.get('done') else []
            if issues:
                result['failed'] += issues
                result['done'] = False
            if not result.get('done'):
                checkpoint.snapshot('blocked', result=result)
        except BaseException as exc:
            # Keep the previous committed checkpoint if the browser itself died.
            try:
                checkpoint.snapshot('interrupted')
            except Exception:
                pass
            if isinstance(exc, Exception) and not isinstance(exc, bw.Busy):
                with db.session() as con:
                    count = (previous['failures'] if previous and previous['fingerprint'] == fingerprint and not retry else 0) + 1
                    message = 'Browser operation interrupted (' + type(exc).__name__ + '); checkpoint preserved. Inspect the draft before retrying.'
                    set_state(con, app_id, status='filling', pending=[{'q':message,'required':True}])
                    con.execute('INSERT OR REPLACE INTO application_attempts(app_id,fingerprint,failures,error,updated_at) VALUES (?,?,?,?,?)', (app_id,fingerprint,count,message,db.now()))
            raise
        result.update(app=app_id, tab=s.tab_id(page), company=company, ats=kind,
                      title=title or page.title()[:80], workspace=workspace)
    with db.session() as con:
        status = "filled" if result.get("done") and not result["pending"] and not result.get("failed") else "filling"
        set_state(con, app_id, status=status, pending=result["pending"])
        failure = bool(result.get('failed'))
        count = (previous['failures'] if previous and previous['fingerprint'] == fingerprint and not retry else 0) + int(failure)
        if any('sign-in rejected' in str(x).lower() for x in result.get('failed', [])):
            count = int(config.get('apply.max_step_failures', 2))
        con.execute('INSERT OR REPLACE INTO application_attempts(app_id,fingerprint,failures,error,updated_at) VALUES (?,?,?,?,?)',
                    (app_id, fingerprint, count if failure else 0, '; '.join(result.get('failed', []))[:500], db.now()))
        if kind == "workday":
            from urllib.parse import urlparse
            con.execute("UPDATE applications SET account=? WHERE id=?", (urlparse(url).netloc, app_id))
        if title is None:
            con.execute("UPDATE applications SET title=COALESCE(title, ?) WHERE id=?", (result["title"], app_id))
    return result


def pending_all(con) -> list[dict]:
    marks = ",".join("?" * len(DONE))
    out = []
    for r in con.execute(f"SELECT * FROM applications WHERE status NOT IN ({marks}) ORDER BY id", DONE):
        for q in json.loads(r["pending"] or "[]"):
            out.append({"app": r["id"], "company": r["company"], **q})
    return out


def snapshot(con, app_id: int) -> int:
    """Capture an open draft without changing it, including Workday and frames.

    Structured identities preserve repeated labels. Capture is recovery evidence,
    not proof that an application is ready. No passwords, SSNs or OTPs are saved.
    """
    from . import ats, browse, browser_workspaces as bw
    from .application_checkpoints import Recorder
    row = get(con, app_id)
    if not row:
        return 0
    bound = bw.binding(app_id)
    workspace = bound['workspace'] if bound else 'default'
    base = row['url'].split('#')[0].split('?')[0].rstrip('/')
    with browse.Session(workspace) as s:
        page = next((p for p in s.pages() if (bound and s.tab_id(p)==bound['tab']) or p.url.split('?')[0].startswith(base)), None)
        if page is None:
            return 0
        adapter = ats.adapter_for(row['ats'])(page, load_bank(), row['company'], app_answers(row), resume=resume_path(load_bank()))
        bw.bind(app_id, workspace, s.tab_id(page))
        recorder = Recorder(app_id, workspace, page, adapter)
        recorder.snapshot('captured')
        return len([f for f in adapter.scan() if not f.get('protected') and f.get('kind') != 'password'])
