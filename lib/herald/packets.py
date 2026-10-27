"""Application packets: the application as data, reviewed before any form.

The first two application rounds (23 and 27 Oct 2026) kept each filled
application only as a live browser tab, and asked the person to review it by
driving that tab hours later. Tabs were lost to an out-of-memory restart,
Workday sessions expired while they waited ("Something went wrong" on the
next click), and review meant scrolling a remote desktop through multi-page
wizards to find the few answers that needed judgment.

A packet separates what an application says from the act of typing it into a
form:

- **Questions come from the employer's own API where one exists.**
  Greenhouse and Ashby publish every posting's questions and whether it is
  still open, so a packet needs no browser at all. For other systems the
  packet is built from the latest form checkpoint.
- **The packet is what the person reviews**, on the review desk (served by
  bin/herald-browser-review at /groups/desk). Routine answers from the
  profile collapse into one line; essays, guesses and anything new are shown
  in full and editable.
- **Filling happens after approval, one application at a time,** by the
  `herald apply worker` process. Approved answers are written into the
  application's own answers, which the adapters read before the bank, so the
  form receives exactly what was approved.
- **Submitting stays with the person.** The desk shows the live form at its
  final page; they press Submit there and record it.

States: draft (a required answer is missing), review (complete, awaiting
the person), approved (queued for filling), filling, ready (the form is on
its final page and verified), blocked (filling stopped, with reasons),
submitted, skipped.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets as _random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

from . import apply as ap
from . import config, db

STATES = ("draft", "review", "approved", "filling", "ready", "blocked", "submitted", "skipped")
OPEN = ("draft", "review", "approved", "filling", "ready", "blocked")

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0 Safari/537.36"


# --- questions from the employer's API ------------------------------------------

def _get_json(url, body=None, headers=None, timeout=20):
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None,
                                 {"User-Agent": UA, "Accept": "application/json",
                                  **({"Content-Type": "application/json"} if body is not None else {}),
                                  **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _greenhouse_ids(url):
    u = urlsplit(url)
    q = parse_qs(u.query)
    parts = [p for p in u.path.split("/") if p]
    if "greenhouse.io" not in u.netloc:
        if "gh_jid" in q and "for" in q:
            return q["for"][0], q["gh_jid"][0]
        return None
    board = q.get("for", [parts[0] if parts else None])[0]
    job = q.get("token", q.get("gh_jid", [None]))[0]
    if not job and "jobs" in parts and parts.index("jobs") + 1 < len(parts):
        job = parts[parts.index("jobs") + 1]
    return (board, job) if board and job else None


def _greenhouse(url):
    ids = _greenhouse_ids(url)
    if not ids:
        return None
    board, job = ids
    try:
        d = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job}?questions=true")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"open": False, "title": None, "questions": []}
        raise
    kinds = {"input_text": "text", "textarea": "textarea", "input_file": "file",
             "multi_value_single_select": "select", "multi_value_multi_select": "checks",
             "input_hidden": None}
    out = []

    def add(q, section):
        fields = [f for f in q.get("fields", []) if kinds.get(f.get("type"))]
        if not fields:
            return
        f = fields[0]  # a file question offers paste-as-text as its second field
        kind = kinds[f["type"]]
        options = [v["label"] for v in f.get("values", [])]
        if kind == "select" and set(map(str.lower, options)) <= {"yes", "no"}:
            kind = "radio"
        out.append({"key": f"gh:{f.get('name')}", "label": q.get("label", "").strip(),
                    "kind": kind, "required": bool(q.get("required")), "options": options,
                    "section": section})

    for q in d.get("questions", []):
        add(q, "")
    for q in d.get("location_questions", []) or []:
        add(q, "")
    for block in d.get("compliance", []) or []:
        for q in block.get("questions", []) or []:
            add(q, "Voluntary self-identification")
    demo = d.get("demographic_questions") or {}
    for q in demo.get("questions", []) or []:
        out.append({"key": f"ghd:{q.get('id')}", "label": q.get("label", "").strip(),
                    "kind": "checks" if q.get("type") == "multi_value_multi_select" else "select",
                    "required": bool(q.get("required")),
                    "options": [a.get("label") for a in q.get("answer_options", [])],
                    "section": "Voluntary self-identification"})
    return {"open": True, "title": d.get("title"), "questions": out}


ASHBY_QUERY = """query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
 jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) {
  id title isListed
  applicationForm { sections { title fieldEntries { ... on FormFieldEntry { id isRequired field } } } }
 } }"""


def _ashby(url):
    u = urlsplit(url)
    if "ashbyhq.com" not in u.netloc:
        return None
    parts = [p for p in u.path.split("/") if p]
    if len(parts) < 2:
        return None
    d = _get_json("https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting",
                  {"operationName": "ApiJobPosting", "query": ASHBY_QUERY,
                   "variables": {"organizationHostedJobsPageName": parts[0], "jobPostingId": parts[1]}},
                  {"Origin": "https://jobs.ashbyhq.com", "apollographql-client-name": "frontend_non_user"})
    jp = (d.get("data") or {}).get("jobPosting")
    if not jp:
        return {"open": False, "title": None, "questions": []}
    kinds = {"String": "text", "Email": "text", "Phone": "text", "LongText": "textarea",
             "File": "file", "Boolean": "radio", "ValueSelect": "select",
             "MultiValueSelect": "checks", "Location": "text", "Date": "date", "Number": "text",
             "SocialLink": "text"}
    out = []
    for s in (jp.get("applicationForm") or {}).get("sections", []):
        for e in s.get("fieldEntries", []):
            f = e.get("field") or {}
            kind = kinds.get(f.get("type"), "text")
            options = ["Yes", "No"] if kind == "radio" else \
                [v.get("label") for v in f.get("selectableValues") or []]
            out.append({"key": f"ashby:{f.get('path') or f.get('id') or e.get('id')}",
                        "label": (f.get("title") or "").strip(), "kind": kind,
                        "required": bool(e.get("isRequired")), "options": options,
                        "section": s.get("title") or ""})
    return {"open": bool(jp.get("isListed", True)), "title": jp.get("title"), "questions": out}


def fetch_questions(url):
    """{"open", "title", "questions"} from the employer's API, or None when
    the system has no public question API."""
    for fn in (_greenhouse, _ashby):
        got = fn(url)
        if got is not None:
            return got
    return None


# --- questions from a form checkpoint -------------------------------------------

def _from_checkpoint(app_id):
    from .application_checkpoints import latest, protected, read_value
    cp = latest(app_id)
    if not cp:
        return None
    payload = cp["payload"]
    # What Herald wrote comes first (with the value it intended), then every
    # field the form showed. A Workday review page is read-only text, so its
    # questions exist only in the writes made on earlier pages.
    writes = payload.get("writes") or {}
    fields = [dict(w["field"], observed_intended=w.get("intended")) for w in writes.values() if w.get("field")]
    fields += list((payload.get("field_history") or {}).values()) or payload.get("fields", [])
    out, seen = [], set()
    for f in fields:
        if f.get("protected") or protected(f) or not f.get("label"):
            continue
        key = f.get("key") or f["label"]
        if key in seen:
            continue
        seen.add(key)
        options = [o["label"] if isinstance(o, dict) else o for o in f.get("options") or []]
        observed = f["observed_intended"] if f.get("observed_intended") not in (None, "") else read_value(f)
        if isinstance(observed, list):
            observed = observed[0] if len(observed) == 1 else (observed or None)
        out.append({"key": key, "label": f["label"], "kind": f.get("kind", "text"),
                    "required": bool(f.get("required")), "options": options,
                    "section": f.get("section", ""), "observed": observed or None})
    return {"open": None, "title": None, "questions": out, "checkpoint": cp["id"]}


# --- the packet -----------------------------------------------------------------

def _load(con, app_id):
    r = con.execute("SELECT * FROM application_packets WHERE app_id=?", (app_id,)).fetchone()
    if not r:
        return None
    p = dict(r)
    p["questions"] = json.loads(p["questions"] or "[]")
    p["blockers"] = json.loads(p["blockers"] or "[]")
    return p


def load(app_id):
    with db.session() as con:
        return _load(con, app_id)


def _save(con, app_id, **cols):
    if "questions" in cols:
        cols["questions"] = json.dumps(cols["questions"], ensure_ascii=False)
    if "blockers" in cols:
        cols["blockers"] = json.dumps(cols["blockers"], ensure_ascii=False)
    cols["updated_at"] = db.now()
    if not con.execute("SELECT 1 FROM application_packets WHERE app_id=?", (app_id,)).fetchone():
        con.execute("INSERT INTO application_packets(app_id, state, updated_at) VALUES (?, 'draft', ?)",
                    (app_id, cols["updated_at"]))
    con.execute(f"UPDATE application_packets SET {', '.join(k + '=?' for k in cols)} WHERE app_id=?",
                (*cols.values(), app_id))


def _answer(bank, q, company, answers):
    """(value, source) for one question. Sources: you (edited on the desk),
    application (written for this employer), bank, profile, draft (what the
    form held at its last checkpoint), or None."""
    from . import ats
    label = re.sub(r"\s*\*+\s*$", "", q["label"])
    key = ap.norm(label)
    general, gsrc = None, None
    if ap.has_answer(bank, label, company, None):
        general, gsrc = ap.lookup(bank, label, company, None)[0], "bank"
    elif ats.profile_value(bank, label) is not None:
        general, gsrc = ats.profile_value(bank, label), "profile"
    if key in answers:
        v = answers[key]
        # An earlier session saved many routine answers per application. One
        # that says the same as the profile is routine; one that differs is
        # exactly what the person should see (a name saved in capitals, 27 Oct 2026).
        # Identity fields (name, email, links) always come from the profile:
        # an old per-application copy is how a capitalised name reached three forms.
        if gsrc == "profile" or (gsrc and _same(q, v, general)):
            return general, gsrc
        q["general"] = _display(q, general) if gsrc else None
        return v, "application"
    obs = q.get("observed")
    if obs not in (None, "", []):
        # The form already holds a value (a fill wrote it, or the page kept
        # it). Show what is there, never a re-derived guess: a work entry's
        # "Location" or "From" is not the profile's, and resolving it again
        # by label showed the school's years on every job (27 Oct 2026).
        if gsrc and _same(q, obs, general):
            return general, gsrc
        if _in_history(bank, obs):
            return obs, "bank"
        if _in_profile(bank, obs):
            return obs, "profile"
        return obs, "draft"
    if gsrc:
        return general, gsrc
    return None, None


def _canon(value):
    v = str(value).strip().casefold()
    v = re.sub(r"^https?://(www\.)?", "", v).rstrip("/")
    digits = re.sub(r"\D", "", v)
    if len(digits) >= 10 and len(digits) >= len(re.sub(r"[\s()+.-]", "", v)) - 1:
        return digits[-10:]  # a phone number, however it is punctuated
    return v


STATE_NAMES = {"MD": "Maryland", "VA": "Virginia", "DC": "District of Columbia"}


def _in_profile(bank, value):
    """Whether a value the form holds is one of the profile's own values in
    another format: 2407568220 for a dashed phone, United States of America
    (+1) for the country."""
    v = _canon(value)
    prof = bank.get("profile", {})
    vals = [x for x in prof.values() if isinstance(x, str)]
    vals += [STATE_NAMES.get(x, "") for x in vals]
    for x in vals:
        c = _canon(x)
        if c and (c == v or (len(c) > 3 and v.startswith(c))):
            return True
    return v in ("mobile", "english")


def _in_history(bank, value):
    """Whether a value comes from the bank's work or education entries."""
    v = str(value).strip().casefold()
    if not v:
        return False
    for e in bank.get("experience", []) + bank.get("education", []):
        for x in e.values():
            if isinstance(x, str) and x.strip().casefold() == v:
                return True
    return False


def _same(q, a, b):
    return _canon(_display(q, a)) == _canon(_display(q, b))


def _display(q, value):
    """What the form will show for a stored answer: an option label when the
    stored answer is a pattern over options, otherwise plain text."""
    from . import ats
    if value is None:
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return [_display(q, v) for v in value]
    if q.get("options"):
        hit = ap.best_option(value, q["options"])
        if hit:
            return hit
    return ats.plain(value)


def _flag(q):
    if q["kind"] == "file":
        return "routine"
    if q.get("source") is None:
        return "missing" if q.get("required") or q["kind"] == "textarea" else "routine"
    if q["source"] in ("you",):
        return "routine"
    if q["source"] in ("application", "draft"):
        return "review"
    if q.get("options") and q.get("display") is None:
        return "review"  # a stored answer that matches none of this form's options
    return "routine"


def build(app_id, *, refresh=False):
    """Create or update the packet. Edits made on the desk survive a rebuild."""
    with db.session() as con:
        row = ap.get(con, app_id)
        if not row:
            raise SystemExit(f"no application {app_id}")
        old = _load(con, app_id)
    if old and old["state"] in ("submitted", "skipped", "filling") and not refresh:
        return old
    got = None
    error = None
    try:
        got = fetch_questions(row["url"])
    except Exception as e:  # the API is a convenience; the checkpoint still works
        error = f"{type(e).__name__}"
    source = "api" if got else "checkpoint"
    seen = _from_checkpoint(app_id)
    if not got:
        got = seen
    elif seen:
        # What the old form held, matched by question text, so an answer typed
        # into a draft that never reached the bank is not lost.
        held = {ap.norm(q["label"]): q.get("observed") for q in seen["questions"]}
        for q in got["questions"]:
            if held.get(ap.norm(q["label"])) not in (None, "", []):
                q["observed"] = held[ap.norm(q["label"])]
    if not got or not got["questions"]:
        # No question API and no recorded writes: the questions are only
        # learnt by filling the form, which needs no approval (nothing is
        # submitted). The person reviews the packet once it is ready.
        with db.session() as con:
            state = old["state"] if old and old["state"] in ("approved", "filling", "blocked") else "approved"
            _save(con, app_id, state=state, source="none", questions=[],
                  approved_at=db.now() if state == "approved" else (old or {}).get("approved_at"),
                  blockers=(old or {}).get("blockers") if state == "blocked" else
                  ["Herald fills this form first to learn its questions; review them when it is ready."])
            return _load(con, app_id)
    if got.get("open") is False:
        with db.session() as con:
            ap.set_state(con, app_id, status="closed", note="posting closed (employer API)")
            _save(con, app_id, state="skipped", source=source, questions=[],
                  blockers=["The employer's API says this posting is closed."])
            return _load(con, app_id)
    bank = ap.load_bank()
    answers = ap.app_answers(row)
    edits = {q["key"]: q for q in (old or {}).get("questions", []) if q.get("source") == "you"}
    questions = []
    for q in got["questions"]:
        if q["key"] in edits:
            q.update(answer=edits[q["key"]]["answer"], source="you")
        else:
            v, src = _answer(bank, q, row["company"], answers)
            q.update(answer=v, source=src)
        q["display"] = _display(q, q["answer"])
        q["flag"] = _flag(q)
        questions.append(q)
    missing = [q for q in questions if q["flag"] == "missing"]
    if old and old["state"] in ("approved", "ready", "blocked") and (not refresh or source != "api"):
        state = old["state"]
    elif source != "api":
        # A checkpoint shows only the pages a fill has reached, so it is not
        # the whole application. Fill first; the person reviews at ready.
        state, missing = "approved", []
    else:
        state = "draft" if missing else "review"
    with db.session() as con:
        _save(con, app_id, state=state, source=source, questions=questions,
              **({"approved_at": db.now(), "blockers": ["Herald fills this form first to learn all "
                  "its questions; review them when it is ready."]} if state == "approved" and source != "api"
                 and not (old and old["state"] == "approved") else
                 {} if state in ("blocked", "ready", "approved") and old else
                 {"blockers": [f"Needs an answer: {q['label']}" for q in missing]}))
        return _load(con, app_id)


def edit(app_id, values: dict):
    """Store the person's edits. `values` maps question key to the new answer."""
    with db.session() as con:
        p = _load(con, app_id)
        if not p:
            raise SystemExit("no packet")
        for q in p["questions"]:
            if q["key"] in values:
                v = values[q["key"]]
                q["answer"], q["source"] = (v if v != "" else None), ("you" if v != "" else None)
                q["display"] = _display(q, q["answer"])
                q["flag"] = _flag(q)
        missing = [q for q in p["questions"] if q["flag"] == "missing"]
        state = p["state"] if p["state"] in ("submitted", "skipped") else ("draft" if missing else "review")
        _save(con, app_id, questions=p["questions"], state=state,
              blockers=[f"Needs an answer: {q['label']}" for q in missing])
        return _load(con, app_id)


def approve(app_id):
    """Queue the approved answers for filling. Returns the packet."""
    with db.session() as con:
        p = _load(con, app_id)
        if not p or p["state"] not in ("review", "blocked", "ready"):
            raise SystemExit("only a complete packet can be approved")
        # Written as this application's own answers, which every adapter reads
        # before the bank: the form gets what was approved, word for word.
        for q in p["questions"]:
            if q["answer"] is not None and q["source"] in ("you", "application", "draft") \
                    and q["kind"] != "file":
                ap.store_answer(con, label=q["label"], value=q["answer"], app_id=app_id)
        # priority 2: the person asked, so a step that failed before may be
        # tried once more. Herald's own first fills (0) never retry one.
        _save(con, app_id, state="approved", approved_at=db.now(), blockers=[], priority=2)
    start_worker()
    return load(app_id)


def skip(app_id, why="skipped on the review desk"):
    with db.session() as con:
        _save(con, app_id, state="skipped")
        ap.set_state(con, app_id, status="withdrawn", note=why)


def submitted(app_id, *, close=True):
    """The person pressed Submit on the employer's page."""
    with db.session() as con:
        _save(con, app_id, state="submitted")
        ap.set_state(con, app_id, status="submitted", pending=[], note="submitted by you from the review desk")
    if close:
        _close_tab(app_id)
    start_worker()  # a free slot: fill the next approved packet


def start_closer(app_id):
    """Close a submitted application's tab from a separate process."""
    subprocess.Popen([sys.executable, str(config.ROOT / "bin" / "herald"), "apply", "close", str(app_id)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                     start_new_session=True, cwd=str(config.ROOT))


def _close_tab(app_id):
    from . import browse, browser_workspaces as bw
    b = bw.binding(app_id)
    if not b or not b.get("tab"):
        return
    try:
        with browse.Session(b["workspace"]) as s:
            s.tab(b["tab"]).close()
    except (Exception, SystemExit):
        pass  # a tab that is already gone is the goal


# --- the worker ------------------------------------------------------------------

def _lock_path():
    p = config.HOME / "run" / "apply-worker.lock"
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def start_worker():
    """Start `herald apply worker` unless one is running. It exits when the
    queue is empty, so nothing stays resident between rounds."""
    fd = os.open(_lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    herald = config.ROOT / "bin" / "herald"
    log = open(config.HOME / "run" / "apply-worker.log", "a")
    subprocess.Popen([sys.executable, str(herald), "apply", "worker"], stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, start_new_session=True, cwd=str(config.ROOT))
    return True


def _desk_active():
    """The person is at the desk's live view: filling another tab would pull
    the browser's focus away from the form they are submitting."""
    try:
        return time.time() - (config.HOME / "run" / "desk-live").stat().st_mtime < 120
    except OSError:
        return False


def touch_desk():
    p = config.HOME / "run" / "desk-live"
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    p.touch()


def _next(con, *, wanted=None):
    if wanted:
        return con.execute("SELECT app_id FROM application_packets WHERE app_id=? AND state='approved'",
                           (wanted,)).fetchone()
    return con.execute("SELECT app_id FROM application_packets WHERE state='approved' "
                       "ORDER BY COALESCE(priority,0) DESC, approved_at LIMIT 1").fetchone()


def worker():
    """Fill approved packets one at a time, until none are left."""
    fd = os.open(_lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("worker already running")
        return 0
    try:
        while True:
            with db.session() as con:
                urgent = con.execute("SELECT app_id FROM application_packets WHERE state='approved' "
                                     "AND priority=1 LIMIT 1").fetchone()
                ready = con.execute("SELECT count(*) FROM application_packets WHERE state='ready'").fetchone()[0]
                full = ready >= int(config.get("apply.ready_limit", 8))
                nxt = urgent or (None if _desk_active() or full else _next(con))
                if nxt is None and not con.execute(
                        "SELECT 1 FROM application_packets WHERE state='approved'").fetchone():
                    return 0
            if nxt is None:
                if full and not _desk_active():
                    return 0  # enough forms wait open; the next submit restarts the worker
                time.sleep(10)  # the person is submitting; wait for them
                continue
            fill(nxt[0])
    finally:
        os.close(fd)


def fill(app_id):
    """Fill one approved packet into its form and stop on the final page."""
    from . import browser_workspaces as bw
    with db.session() as con:
        row = ap.get(con, app_id)
        retry = _load(con, app_id)["priority"] > 0
        _save(con, app_id, state="filling", priority=0)
    print(time.strftime("%H:%M:%S"), "filling", app_id, row["company"], flush=True)
    try:
        r = ap.run(row["url"], workspace="default", retry=retry)
    except (Exception, SystemExit) as e:
        msg = str(e)[:300] if isinstance(e, (bw.Busy, SystemExit)) else \
            f"The browser stopped while filling ({type(e).__name__}). Nothing was submitted."
        with db.session() as con:
            _save(con, app_id, state="blocked", blockers=[msg])
        print("  blocked:", msg, flush=True)
        _close_tab(app_id)
        return
    problems = [q["q"] for q in r.get("pending", [])] + list(r.get("failed", []))
    try:
        refreshed = _from_checkpoint(app_id)
        if refreshed and refreshed["questions"] and load(app_id)["source"] != "api":
            bank, row2 = ap.load_bank(), None
            with db.session() as con:
                row2 = ap.get(con, app_id)
            qs = []
            for q in refreshed["questions"]:
                v, src = _answer(bank, q, row2["company"], ap.app_answers(row2))
                q.update(answer=v, source=src)
                q["display"] = _display(q, v)
                q["flag"] = _flag(q)
                qs.append(q)
            with db.session() as con:
                _save(con, app_id, questions=qs, source="checkpoint")
    except Exception as e:  # the fill result stands even if the summary fails
        print("  packet refresh failed:", type(e).__name__, flush=True)
    with db.session() as con:
        if r.get("done") and not problems:
            _save(con, app_id, state="ready", filled_at=db.now(), blockers=[])
        else:
            _save(con, app_id, state="blocked", blockers=problems or ["The form did not reach its final page."])
    if not (r.get("done") and not problems):
        # A stopped form keeps nothing a tab is needed for: its checkpoint
        # holds the answers and the desk reopens it on demand. Leaving every
        # stopped tab open filled the browser's tab cap on the first run.
        _close_tab(app_id)
    print("  ->", "ready" if r.get("done") and not problems else f"blocked: {problems[:3]}", flush=True)


def check_ready(app_id):
    """Before the person opens a ready form: is its tab still on a live final
    page? Workday sessions expire while a tab waits, so a stale one is
    queued for an immediate refill instead of being shown broken."""
    from . import browse, browser_workspaces as bw
    b = bw.binding(app_id)
    ok, why = False, "its tab is gone"
    if b and b.get("tab"):
        try:
            with browse.Session(b["workspace"]) as s:
                page = s.tab(b["tab"])
                text = page.evaluate("()=>document.body.innerText.slice(0,4000)")
                final = page.get_by_role("button", name=re.compile(
                    r"^Submit( Application)?$|^Send Application$|^Apply$", re.I))
                if re.search(r"something went wrong|session (has )?expired|signed out|sign in", text, re.I):
                    why = "the employer's session expired"
                elif not final.count():
                    why = "the form is no longer on its final page"
                else:
                    page.bring_to_front()
                    return True, None
        except (Exception, SystemExit) as e:
            why = f"the browser could not show it ({type(e).__name__})"
    with db.session() as con:
        _save(con, app_id, state="approved", priority=1, blockers=[f"Refilling: {why}."])
    start_worker()
    return False, why


def show(app_id):
    """Bring an application's tab to the front (opening it if it is gone) so
    the person can act on it themselves: a sign-in, a CAPTCHA, a field the
    adapter could not set. Returns the tab id."""
    from . import browse, browser_workspaces as bw
    with db.session() as con:
        row = ap.get(con, app_id)
    b = bw.binding(app_id)
    with browse.Session("default") as s:
        page = None
        if b and b.get("tab"):
            try:
                page = s.tab(b["tab"])
            except (Exception, SystemExit):
                page = None
        if page is None:
            page = s.ctx.new_page()
            page.goto(row["url"], wait_until="domcontentloaded", timeout=60000)
        page.bring_to_front()
        tab = s.tab_id(page)
        bw.bind(app_id, "default", tab)
        return tab


# --- the desk link ----------------------------------------------------------------

def desk_token():
    """The desk's long-lived token, stored hashed. A new one is minted (and
    the old one revoked) each time the person asks for the link."""
    import hashlib
    token = _random.token_urlsafe(24)
    p = config.HOME / "run" / "desk-token"
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    p.write_text(hashlib.sha256(token.encode()).hexdigest())
    os.chmod(p, 0o600)
    return token


def desk_ok(token):
    import hashlib
    import hmac
    try:
        want = (config.HOME / "run" / "desk-token").read_text().strip()
    except OSError:
        return False
    return bool(token) and hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), want)


def desk_url(token):
    from . import browse
    base = browse.view_base()
    return f"{base}/groups/desk/{token}" if base else None


def summary(con):
    rows = con.execute("""SELECT a.id, a.company, a.title, a.ats, a.url, p.state, p.blockers,
                                 p.questions, p.updated_at
                          FROM application_packets p JOIN applications a ON a.id=p.app_id
                          ORDER BY a.id""").fetchall()
    out = []
    for r in rows:
        qs = json.loads(r["questions"] or "[]")
        out.append({"id": r["id"], "company": r["company"], "title": r["title"], "ats": r["ats"],
                    "url": r["url"], "state": r["state"], "blockers": json.loads(r["blockers"] or "[]"),
                    "review": sum(q["flag"] == "review" for q in qs),
                    "missing": sum(q["flag"] == "missing" for q in qs), "count": len(qs)})
    return out
