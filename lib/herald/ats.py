"""Adapters that turn an application page into fields and fill them.

Two adapters cover the systems seen so far:

- `Generic` reads any single-page form the accessible way: every input,
  textarea and select in the document *and in open shadow roots*
  (SmartRecruiters builds its form from web components, which the first
  batch could not see), labelled by label[for], aria-labelledby, aria-label
  or the enclosing question, with radio and checkbox groups gathered under
  their question. Greenhouse, Ashby, Lever and SmartRecruiters all fill
  through it.
- `Workday` walks Workday's numbered wizard by its stable
  `data-automation-id="formField-*"` containers, adds work, education and
  website entries from the bank, signs in or creates the account, and stops
  on the Review step.

Neither ever presses a final submit. The generic adapter never clicks a
button at all except to add a row or open a dropdown; the Workday adapter
presses only Next / Save and Continue, and refuses on the last step.
"""

from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urlparse

from . import apply as ap
from . import config

# Profile keys every applicant has, by question label. The values live in the
# person's bank (apply.bank), never here.
PROFILE_RULES = [
    (r"^(legal )?first name|^given name", "first_name"),
    (r"^preferred (first )?name", "preferred_name"),
    (r"^(legal )?last name|^family name|^surname", "last_name"),
    (r"^(full |legal )?(first and last )?name$|^full legal name$|^legal first and last name", "full_name"),
    (r"^(confirm (your )?)?e-?mail", "email"),
    (r"^(mobile |cell )?phone( number)?$", "phone"),
    (r"linkedin", "linkedin"),
    (r"github", "github"),
    (r"^(personal )?(website|portfolio)|^other website", "website"),
    (r"^(address|street)( line 1)?$", "address1"),
    (r"^city$", "city"),
    (r"^(zip|postal)", "postal"),
    (r"^(current )?location( \(city\))?$|^your location$|^city, state", "location"),
    (r"^(school|university|college)( or university)?( name)?$|^school or university", "school"),
    (r"^degree( type)?$", "degree"),
    (r"^(discipline|major|field of study)$", "major"),
    (r"^start date year$|^from$", "start_year"),
    (r"^end date year$|^to( \(actual or expected\))?$", "end_year"),
    (r"^start date month$", "start_month"),
    (r"^end date month$", "end_month"),
    (r"^(graduation|anticipated graduation|expected graduation) date$", "graduation"),
    (r"^current company$|^current employer$", "current_company"),
]

SUBMITTY = re.compile(r"submit|send application|apply$|apply now|finish|complete application", re.I)


def detect(url: str, html_hint: str = "") -> str:
    host = urlparse(url).netloc
    if "myworkdayjobs.com" in host or "workday" in host:
        return "workday"
    for name in ("greenhouse", "ashbyhq", "lever.co", "smartrecruiters", "icims", "eightfold"):
        if name in host or name in html_hint:
            return name.replace("hq", "").replace(".co", "")
    return "generic"


def profile_value(bank: dict, label: str):
    prof = bank.get("profile", {})
    for rx, key in PROFILE_RULES:
        if re.search(rx, label.strip(), re.I) and prof.get(key) not in (None, ""):
            return prof[key]
    return None


def resolve(bank: dict, label: str, company: str | None, answers: dict):
    """(value, found). Bank and per-application answers first, profile second."""
    if ap.has_answer(bank, label, company, answers):
        v, _ = ap.lookup(bank, label, company, answers)
        return v, True
    v = profile_value(bank, label)
    return v, v is not None


# --- the generic adapter -----------------------------------------------------------

SCAN_JS = r"""() => {
  const els = [];
  (function walk(root) {
    root.querySelectorAll('*').forEach(e => {
      if (e.shadowRoot) walk(e.shadowRoot);
      if (e.matches('input, textarea, select')) els.push(e);
    });
  })(document);
  const txt = e => (e ? (e.innerText || e.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const labelOf = e => {
    const root = e.getRootNode();
    let t = '';
    if (e.id) { const l = root.querySelector('label[for="' + CSS.escape(e.id) + '"]'); if (l) t = txt(l); }
    if (!t && e.getAttribute('aria-labelledby'))
      t = e.getAttribute('aria-labelledby').split(' ').map(i => txt(root.getElementById ? root.getElementById(i) : document.getElementById(i))).join(' ');
    if (!t) t = e.getAttribute('aria-label') || '';
    if (!t) { const l = e.closest('label'); if (l) t = txt(l); }
    if (!t && root.host) t = root.host.getAttribute('label') || '';
    if (!t) t = e.getAttribute('placeholder') || '';
    return t.trim();
  };
  const questionOf = e => {
    const fs = e.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg) return txt(lg); }
    const g = e.closest('[role=radiogroup],[role=group]');
    if (g && g.getAttribute('aria-labelledby')) return txt(document.getElementById(g.getAttribute('aria-labelledby').split(' ')[0]));
    if (g && g.getAttribute('aria-label')) return g.getAttribute('aria-label');
    // Climb until the container holds text beyond its options' own labels.
    let n = e.parentElement;
    for (let i = 0; i < 6 && n; i++, n = n.parentElement) {
      const lab = n.querySelector(':scope > label, :scope > legend, :scope > p, :scope > span, :scope > div > label');
      const own = lab && !lab.contains(e) ? txt(lab) : '';
      if (own && own.length > 2) return own;
    }
    return '';
  };
  const out = []; const groups = {};
  els.forEach((e, i) => {
    const type = (e.getAttribute('type') || e.tagName).toLowerCase();
    if (type === 'hidden' || type === 'submit' || type === 'button' || type === 'search') return;
    if (e.getAttribute('aria-hidden') === 'true' && type !== 'file') return;
    const r = e.getBoundingClientRect();
    const vis = r.width > 0 || type === 'file' || type === 'radio' || type === 'checkbox';
    if (!vis) return;
    if (/honeypot|robots/i.test(labelOf(e) + (e.name || ''))) return;
    e.setAttribute('data-hx', i);
    const req = e.required || e.getAttribute('aria-required') === 'true';
    if (type === 'radio' || type === 'checkbox') {
      const q = questionOf(e);
      const key = (type === 'radio' ? (e.name || q) : q) || ('cb' + i);
      if (!groups[key]) { groups[key] = {kind: type === 'radio' ? 'radio' : 'checks', label: q, required: req, options: []}; out.push(groups[key]); }
      groups[key].required = groups[key].required || req;
      groups[key].options.push({hx: i, label: labelOf(e), checked: e.checked});
      return;
    }
    let kind = 'text';
    if (e.tagName === 'TEXTAREA') kind = 'textarea';
    else if (e.tagName === 'SELECT') kind = 'select';
    else if (type === 'file') kind = 'file';
    else if (e.getAttribute('role') === 'combobox') kind = 'combobox';
    if (type === 'password') kind = 'password';
    const o = {hx: i, kind, label: labelOf(e), required: req,
               value: kind === 'file' ? (e.files && e.files.length ? e.files[0].name : '')
                    : kind === 'password' ? (e.value ? '(set)' : '') : (e.value || '')};
    if (kind === 'select') o.options = [...e.options].map(x => x.text.trim()).filter(x => x && !/^select/i.test(x));
    if (kind === 'combobox') {
      const shown = txt(e.closest('[class*=control]') || e.parentElement);
      o.value = shown && !/^select/i.test(shown) ? shown : (e.value || '');
    }
    if (kind === 'file') {
      // Which upload is this? Look at the nearest heading-like text.
      let n = e.parentElement, ctx = '';
      for (let k = 0; k < 6 && n && !ctx; k++, n = n.parentElement) {
        const t = txt(n); if (/resume|cv|cover|transcript/i.test(t)) ctx = t.slice(0, 80);
      }
      o.label = o.label || ctx;
    }
    out.push(o);
  });
  return out;
}"""


class Generic:
    name = "generic"

    def __init__(self, page, bank: dict, company: str | None, answers: dict, *, resume: str):
        self.page, self.bank, self.company, self.answers = page, bank, company, answers
        self.resume = resume

    def scan(self) -> list[dict]:
        found = []
        for fr in self.page.frames:
            try:
                for f in fr.evaluate(SCAN_JS):
                    f["frame"] = fr
                    found.append(f)
            except Exception:
                continue
        return found

    def loc(self, f, hx=None):
        return f["frame"].locator(f'[data-hx="{hx if hx is not None else f["hx"]}"]').first

    def options_of(self, f) -> list[str]:
        if f.get("options") and f["kind"] != "radio" and f["kind"] != "checks":
            return f["options"]
        if f["kind"] in ("radio", "checks"):
            return [o["label"] for o in f["options"]]
        if f["kind"] == "combobox":
            el = self.loc(f)
            el.click()
            self.page.wait_for_timeout(600)
            opts = [o.inner_text().strip() for o in self.page.locator("[role=option]:visible").all()[:80]]
            self.page.keyboard.press("Escape")
            return opts
        return []

    def set(self, f, value) -> bool:
        k = f["kind"]
        if value is None:
            return True  # deliberately blank
        if k == "file":
            path = self.resume if value in (True, "resume") else str(value)
            self.loc(f).set_input_files(path)
            self.page.wait_for_timeout(2500)
            return True
        if k in ("text", "textarea"):
            el = self.loc(f)
            el.fill(plain(value))
            try:
                el.press("Tab")
            except Exception:
                pass
            return True
        if k == "select":
            o = ap.best_option(value, f.get("options") or [])
            if not o:
                return False
            self.loc(f).select_option(label=o)
            return True
        if k == "radio":
            o = ap.best_option(value, [x["label"] for x in f["options"]])
            hit = next((x for x in f["options"] if x["label"] == o), None)
            if not hit:
                return False
            self.loc(f, hit["hx"]).check(force=True)
            return True
        if k == "checks":
            wants = value if isinstance(value, list) else [value]
            if len(f["options"]) == 1 and isinstance(value, bool):
                self.loc(f, f["options"][0]["hx"]).set_checked(value, force=True)
                return True
            ok = False
            for w in wants:
                o = ap.best_option(w, [x["label"] for x in f["options"]])
                hit = next((x for x in f["options"] if x["label"] == o), None)
                if hit:
                    self.loc(f, hit["hx"]).set_checked(True, force=True)
                    ok = True
            return ok
        if k == "combobox":
            return self._combo(f, str(value))
        return False

    def _combo(self, f, want: str) -> bool:
        el = self.loc(f)
        el.click()
        self.page.wait_for_timeout(600)
        opts = self.page.locator("[role=option]:visible")

        def pick():
            labels = [o.inner_text().strip() for o in opts.all()[:150]]
            o = ap.best_option(want, labels)
            if o:
                opts.nth(labels.index(o)).click()
                self.page.wait_for_timeout(300)
                return True
            return False
        if pick():
            return True
        # Searchable lists (schools, cities) show matches only after typing,
        # and a trailing space returned nothing on 23 Oct 2026.
        term = re.sub(r"\s+", " ", re.sub(r"[\^$\\.*|()?\[\]+]", " ", want.split("|")[0]))[:28].strip()
        try:
            el.fill(term)
        except Exception:
            el.type(term, delay=30)
        for _ in range(10):
            self.page.wait_for_timeout(500)
            if pick():
                return True
        self.page.keyboard.press("Escape")
        return False

    def fill(self) -> dict:
        filled, pending, failed = [], [], []
        resume_done = False
        for f in self.scan():
            label = (f.get("label") or "").strip()
            k = f["kind"]
            if k == "file":
                if re.search(r"cover|transcript", label, re.I):
                    continue  # never the resume in a cover-letter slot
                if f.get("value") or resume_done:
                    resume_done = True
                    continue
                if not label or re.search(r"resume|cv|attach", label, re.I):
                    self.set(f, "resume")
                    resume_done = True
                    filled.append("resume")
                continue
            if not label:
                continue
            already = (f.get("value") not in (None, "")) if k not in ("radio", "checks") else \
                any(o["checked"] for o in f["options"])
            if already:
                continue
            value, found = resolve(self.bank, label, self.company, self.answers)
            if not found:
                if f.get("required") or k == "textarea":
                    pending.append(self._pending(f, label))
                continue
            try:
                ok = self.set(f, value)
            except Exception as e:
                ok = False
                failed.append(f"{label}: {type(e).__name__}")
            if ok:
                filled.append(label)
            elif f.get("required"):
                pending.append(self._pending(f, label, note=f"stored answer {value!r} matched no option"))
        return {"filled": filled, "pending": pending, "failed": failed, "done": True}

    def _pending(self, f, label, note=None) -> dict:
        p = {"q": label, "kind": f["kind"], "required": bool(f.get("required"))}
        try:
            opts = self.options_of(f)
        except Exception:
            opts = []
        if opts:
            p["options"] = opts[:40]
        if note:
            p["note"] = note
        return p


# --- Workday ------------------------------------------------------------------------

WD_SCAN = r"""() => {
  const out = [];
  document.querySelectorAll('[data-automation-id^="formField-"]').forEach((f, i) => {
    if (f.getBoundingClientRect().width === 0) return;
    const lab = ((f.querySelector('label, legend') || {}).innerText || '').replace(/\s+/g, ' ').replace(/\*$/, '').trim();
    const id = f.getAttribute('data-automation-id').slice(10);
    let kind = 'text', value = '';
    const btn = f.querySelector('button[aria-haspopup="listbox"]');
    const prompt = f.querySelector('[data-automation-id="multiselectInputContainer"]');
    const radios = f.querySelectorAll('input[type=radio]');
    const cbs = f.querySelectorAll('input[type=checkbox]');
    const ta = f.querySelector('textarea');
    const inp = f.querySelector('input:not([type=hidden]):not([type=radio]):not([type=checkbox])');
    const date = f.querySelector('[data-automation-id*="dateSection"]');
    let options = null;
    if (btn) { kind = 'dropdown'; value = btn.innerText.trim(); if (/^select one$/i.test(value)) value = ''; }
    else if (prompt) { kind = 'prompt'; value = [...f.querySelectorAll('[data-automation-id="selectedItem"]')].map(e => e.innerText.trim()).join('; '); }
    else if (radios.length) { kind = 'radio'; options = [...radios].map(r => ((f.querySelector('label[for="'+r.id+'"]')||{}).innerText||'').trim());
      const c = [...radios].find(r => r.checked); value = c ? 'set' : ''; }
    else if (cbs.length > 1) { kind = 'checks'; options = [...cbs].map(r => ((f.querySelector('label[for="'+r.id+'"]')||{}).innerText||'').trim());
      value = [...cbs].some(c => c.checked) ? 'set' : ''; }
    else if (cbs.length === 1 && !inp) { kind = 'check'; value = cbs[0].checked ? 'set' : ''; }
    else if (date) { kind = 'date'; value = [...f.querySelectorAll('input')].map(x => x.value).join('/').replace(/^\/+|\/+$/g, ''); }
    else if (ta) { kind = 'textarea'; value = ta.value; }
    else if (inp) { kind = inp.type === 'file' ? 'file' : (inp.type === 'password' ? 'password' : 'text');
      // A password's value never leaves the page: on 23 Oct 2026 a debugging
      // dump printed a new account's password into a session transcript.
      value = inp.type === 'password' ? (inp.value ? '(set)' : '') : inp.value; }
    const req = /\*/.test((f.querySelector('label, legend') || {}).innerText || '');
    f.setAttribute('data-wd-i', i);
    out.push({i, id, label: lab, kind, value, options, required: req});
  });
  return out;
}"""

# Workday field ids that map straight onto the profile.
WD_IDS = {
    "legalName--firstName": "first_name", "legalName--lastName": "last_name",
    "addressLine1": "address1", "city": "city", "postalCode": "postal",
    "phoneNumber": "phone_digits", "countryRegion": "state", "phoneType": "phone_type",
    "school": "school", "degree": "degree_workday", "fieldOfStudy": "major",
    "firstYearAttended": "start_year", "lastYearAttended": "end_year",
    "gradeAverage": "gpa",
}


class Workday:
    name = "workday"

    def __init__(self, page, bank, company, answers, *, resume: str):
        self.page, self.bank, self.company, self.answers = page, bank, company, answers
        self.resume = resume
        self.host = urlparse(page.url).netloc

    # -- helpers
    def body(self) -> str:
        return self.page.inner_text("body")

    def step(self) -> tuple[int, int, str]:
        m = re.search(r"current step (\d+) of (\d+)\s*\n?\s*([^\n]+)", self.body())
        return (int(m.group(1)), int(m.group(2)), m.group(3).strip()) if m else (0, 0, "")

    def box(self, f):
        return self.page.locator(f'[data-wd-i="{f["i"]}"]')

    def click(self, loc):
        # Workday lays a transparent click_filter div over its buttons, so a
        # normal click waits forever for the button to be hittable. Fall back
        # to a mouse click at the button's centre, which lands on the filter.
        loc.scroll_into_view_if_needed()
        try:
            loc.click(timeout=4000)
        except Exception:
            bb = loc.bounding_box()
            self.page.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)

    def scan(self):
        return self.page.evaluate(WD_SCAN)

    def options(self, f) -> list[str]:
        if f.get("options"):
            return f["options"]
        b = self.box(f)
        if f["kind"] == "dropdown":
            self.click(b.locator('button[aria-haspopup="listbox"]').first)
            self.page.wait_for_timeout(600)
            o = [x.inner_text().strip() for x in self.page.locator("[role=option]:visible").all()]
            self.page.keyboard.press("Escape")
            return [x for x in o if x and not re.match(r"^select one$", x, re.I)]
        if f["kind"] == "prompt":
            self.click(b.locator('[data-automation-id="multiselectInputContainer"]').first)
            self.page.wait_for_timeout(1000)
            o = [x.inner_text().strip() for x in self.page.locator('[data-automation-id="promptOption"]:visible').all()]
            self.page.keyboard.press("Escape")
            return o[:60]
        return []

    def set(self, f, value) -> bool:
        if value is None:
            return True
        b, k, p = self.box(f), f["kind"], self.page
        if k == "dropdown":
            self.click(b.locator('button[aria-haspopup="listbox"]').first)
            p.wait_for_timeout(600)
            opts = p.locator("[role=option]:visible")
            labels = [x.inner_text().strip() for x in opts.all()]
            o = ap.best_option(value, [x for x in labels if not re.match(r"^select one$", x, re.I)])
            if not o:
                p.keyboard.press("Escape")
                return False
            opts.nth(labels.index(o)).click()
            p.wait_for_timeout(400)
            return True
        if k == "prompt":
            # Click the container: a coordinate click on 23 Oct 2026 landed on
            # the phone-code prompt below. Options can open a submenu
            # ("Internet Job Board" then "Other"); keep choosing until
            # something is selected. "a >> b" names the submenu choice.
            sel = b.locator('[data-automation-id="selectedItem"]')
            before = sel.count()
            self.click(b.locator('[data-automation-id="multiselectInputContainer"]').first)
            p.wait_for_timeout(1000)
            wants = str(value).split(">>")
            for depth in range(3):
                opts = p.locator('[data-automation-id="promptOption"]:visible')
                labels = [x.inner_text().strip() for x in opts.all()[:300]]
                w = wants[min(depth, len(wants) - 1)].strip()
                o = ap.best_option(w, labels)
                if o is None and depth == 0:
                    inp = b.locator("input").first
                    inp.fill(re.sub(r"[^A-Za-z0-9 ]", " ", w.split("|")[0]).strip()[:30])
                    inp.press("Enter")
                    p.wait_for_timeout(1500)
                    labels = [x.inner_text().strip() for x in opts.all()[:300]]
                    o = ap.best_option(w, labels)
                if o is None:
                    o = next((x for x in labels if re.fullmatch(r"other", x, re.I)), labels[0] if labels else None)
                if o is None:
                    break
                opts.nth(labels.index(o)).click()
                p.wait_for_timeout(1200)
                if sel.count() > before:
                    break
            p.keyboard.press("Escape")
            return sel.count() > before
        if k == "radio":
            o = ap.best_option(value, f["options"] or [])
            if not o:
                return False
            b.locator("label").filter(has_text=re.compile("^" + re.escape(o) + "$")).first.click()
            return True
        if k == "checks":
            ok = False
            for w in (value if isinstance(value, list) else [value]):
                o = ap.best_option(w, f["options"] or [])
                if o:
                    b.locator("label").filter(has_text=re.compile("^" + re.escape(o) + "$")).first.click()
                    ok = True
            return ok
        if k == "check":
            cb = b.locator("input[type=checkbox]").first
            if cb.is_checked() != bool(value):
                self.click(cb)
            return True
        if k == "date":
            # Month/year spinbuttons under an overlay: focus and type, never click.
            parts = re.findall(r"\d+", str(value))
            inputs = b.locator("input[role=spinbutton], input").all()
            for inp, part in zip(inputs, parts):
                inp.focus()
                p.keyboard.type(part, delay=40)
            p.keyboard.press("Tab")
            return True
        if k == "file":
            p.locator("input[type=file]").first.set_input_files(self.resume)
            p.wait_for_timeout(4000)
            return True
        el = b.locator("textarea, input").first
        el.fill(plain(value))
        el.press("Tab")
        return True

    def value_for(self, f):
        prof = self.bank.get("profile", {})
        key = WD_IDS.get(f["id"])
        if ap.has_answer(self.bank, f["label"], self.company, self.answers):
            return ap.lookup(self.bank, f["label"], self.company, self.answers)[0], True
        if key and prof.get(key) not in (None, ""):
            return prof[key], True
        return resolve(self.bank, f["label"], self.company, self.answers)

    # -- account
    def account_step(self) -> str | None:
        """Sign in or create the account if the page asks. Returns what happened."""
        p = self.page
        # Workday's account page is itself made of formField containers, so
        # the test is a password box or an account button, not the field count.
        asks = (p.locator("input[type=password]:visible").count()
                or p.locator('[data-automation-id="createAccountLink"]:visible, '
                             '[data-automation-id="createAccountSubmitButton"]:visible, '
                             '[data-automation-id="signInSubmitButton"]:visible').count())
        if not asks:
            return None
        acct = ap.account(self.host)
        email = self.bank.get("profile", {}).get("email")
        pw_fields = p.locator("input[type=password]:visible")
        if acct and acct.get("created", True) is True:
            link = p.locator('[data-automation-id="signInLink"]:visible, button:has-text("Sign In"):visible')
            if p.locator('[data-automation-id="createAccountSubmitButton"]:visible').count() and link.count():
                link.first.click()
                p.wait_for_timeout(1500)
            p.locator('input[data-automation-id="email"]:visible, input[type=email]:visible, input[type=text]:visible').first.fill(acct["email"])
            p.locator("input[type=password]:visible").first.fill(acct["password"])
            btn = p.locator('[data-automation-id="signInSubmitButton"]')
            self.click(btn.first if btn.count() else p.get_by_role("button", name="Sign In").first)
            p.wait_for_timeout(5000)
            return "signed in"
        if not pw_fields.count() >= 2:
            ca = p.locator('[data-automation-id="createAccountLink"]:visible, button:has-text("Create Account"):visible')
            if ca.count():
                ca.first.click()
                p.wait_for_timeout(1500)
        since = dt.datetime.now()
        if acct:  # an earlier attempt stored a password but never created the account
            pw = acct["password"]
        else:
            pw = ap.new_password()
            # Store before creating, so no account can exist that nobody can open.
            ap.save_account(self.host, email, pw, created=False)
            mirror_to_password_manager(p.context, self.host, email, pw, self.company)
        p.locator('input[data-automation-id="email"]:visible, input[type=email]:visible, input[type=text]:visible').first.fill(email)
        for i in range(p.locator("input[type=password]:visible").count()):
            p.locator("input[type=password]:visible").nth(i).fill(pw)
        agree = p.locator('[data-automation-id="createAccountCheckbox"]:visible, input[type=checkbox]:visible')
        if agree.count() and not agree.first.is_checked():
            self.click(agree.first)
        btn = p.locator('[data-automation-id="createAccountSubmitButton"]')
        self.click(btn.first if btn.count() else p.get_by_role("button", name="Create Account").first)
        p.wait_for_timeout(6000)
        if not p.locator("input[type=password]:visible").count():
            ap.save_account(self.host, email, pw, created=True)
        if re.search(r"verify|verification|check your email", self.body(), re.I):
            v = ap.verification(since, hint=None, wait_s=150)
            if v and v.get("link"):
                vp = p.context.new_page()
                vp.goto(v["link"], wait_until="domcontentloaded")
                vp.wait_for_timeout(4000)
                vp.close()
                p.reload()
                p.wait_for_timeout(4000)
                return self.account_step() or "created and verified"
            return "created; waiting on email verification"
        return "created"

    def entries(self, section: str, items: list[dict], fields: dict) -> int:
        """Add repeated entries (work experience, websites) once.

        Workday's Add buttons carry no label (data-automation-id="add-button"
        on every section), so each is matched to the heading of the section
        around it. A section that already has entries is left alone.
        """
        p = self.page
        if any(f["id"] in fields for f in self.scan()):
            return 0
        added = 0
        for item in items:
            tagged = p.evaluate("""(sec) => { let hit = null;
              document.querySelectorAll('[data-automation-id="add-button"]').forEach(btn => {
                let n = btn;
                for (let i = 0; i < 8 && n; i++) { n = n.parentElement;
                  const h = n && n.querySelector('h3,h4,[role=heading]');
                  if (h) { if (h.innerText.trim() === sec && !hit) { hit = btn; } break; } }
              });
              if (hit) hit.setAttribute('data-wd-add', sec);
              return !!hit; }""", section)
            if not tagged:
                break
            self.click(p.locator(f'[data-wd-add="{section}"]').first)
            p.wait_for_timeout(1500)
            fs = [f for f in self.scan() if f["id"] in fields and not f["value"]]
            for fid, key in fields.items():
                f = next((x for x in fs if x["id"] == fid), None)
                if f and item.get(key) not in (None, ""):
                    try:
                        self.set(f, item[key])
                    except Exception:
                        pass
            added += 1
        return added

    def fill(self) -> dict:
        p = self.page
        filled, pending, failed = [], [], []
        # From the posting: Apply, then Apply Manually (predictable, unlike
        # "Autofill with Resume", which guesses at work history).
        start = p.locator('[data-automation-id="adventureButton"]:visible')
        if start.count() and not p.locator('[data-automation-id^="formField-"]').count():
            self.click(start.first)
            p.wait_for_timeout(2500)
            manual = p.locator('[data-automation-id="applyManually"]:visible')
            if not manual.count():
                manual = p.get_by_role("button", name=re.compile(r"^Apply Manually$", re.I))
            if manual.count():
                self.click(manual.first)
                p.wait_for_timeout(4000)
        acct = self.account_step()
        if acct:
            filled.append(f"account: {acct}")
            if "waiting" in acct:
                return {"filled": filled, "pending": [{"q": "verify the account from your email", "kind": "action"}],
                        "failed": failed, "done": False}
        for _ in range(10):
            cur, total, name = self.step()
            if not total:
                break
            # The last step is not always Review (Expedia's 7 of 7 was the
            # disability form), so the stop is the footer button reading
            # Submit, checked before every press below.
            # Progress labels are missing on some tenants, so recognise the
            # experience page by its sections rather than by the step name.
            if re.search(r"experience", name, re.I) or re.search(r"\bWork Experience\b|Resume/CV", self.body()):
                self.entries("Work Experience", self.bank.get("experience", []),
                             {"jobTitle": "title", "companyName": "company", "location": "location",
                              "startDate": "start", "endDate": "end", "roleDescription": "description"})
                self.entries("Websites", [{"url": u} for u in self.bank.get("profile", {}).get("urls", [])],
                             {"url": "url"})
                if p.locator("input[type=file]").count() and "uploaded" not in self.body():
                    p.locator("input[type=file]").first.set_input_files(self.resume)
                    p.wait_for_timeout(4000)
                    filled.append("resume")
            step_pending = []
            for f in self.scan():
                if f["value"] or f["kind"] == "file":
                    continue
                v, found = self.value_for(f)
                if not found:
                    if f["required"] or f["kind"] == "textarea":
                        q = {"q": f["label"], "kind": f["kind"], "required": f["required"], "step": name}
                        try:
                            o = self.options(f)
                            if o:
                                q["options"] = o[:40]
                        except Exception:
                            pass
                        step_pending.append(q)
                    continue
                try:
                    ok = self.set(f, v)
                except Exception as e:
                    ok = False
                    failed.append(f"{f['label']}: {type(e).__name__}")
                (filled if ok else failed).append(f["label"] if ok else f"{f['label']}: no option for {v!r}")
            pending += step_pending
            if step_pending:
                return {"filled": filled, "pending": pending, "failed": failed, "done": False, "step": name}
            nxt = p.locator('[data-automation-id="pageFooterNextButton"]')
            if not nxt.count():
                break
            label = nxt.first.inner_text().strip()
            if SUBMITTY.search(label):
                return {"filled": filled, "pending": pending, "failed": failed, "done": True, "step": name}
            self.click(nxt.first)
            p.wait_for_timeout(5000)
            nc, _, nn = self.step()
            if nc == cur and not re.search(r"Review", self.body()[:3000]):
                errs = [e.inner_text().strip()[:160] for e in p.locator('[data-automation-id="errorMessage"]:visible').all()]
                failed += errs or [f"stuck on {name}"]
                return {"filled": filled, "pending": pending, "failed": failed, "done": False, "step": name}
        cur, total, name = self.step()
        nxt = p.locator('[data-automation-id="pageFooterNextButton"]')
        done = bool(nxt.count()) and bool(SUBMITTY.search(nxt.first.inner_text().strip()))
        return {"filled": filled, "pending": pending, "failed": failed, "done": done, "step": name}


def plain(value) -> str:
    """The text to type for an answer written as a pattern for picking options.

    The bank stores choices as regexes ("Example University.*North
    Campus|Example U"); a free-text box must get words, not the regex, which
    a Workday school box received on 23 Oct 2026.
    """
    v = str(value)
    if not re.search(r"[|^$*\\]", v):
        return v
    v = v.split("|")[0]
    v = v.replace(".*", ", ").replace("^", "").replace("$", "").replace("\\", "")
    return re.sub(r"\s+", " ", v).strip(" ,")


def mirror_to_password_manager(context, host: str, email: str, pw: str, company: str | None) -> bool:
    """Save a new login where the person keeps theirs (apply.password_manager).

    Only the Bitwarden browser extension is supported: it must be installed
    and unlocked in the profile browser. Herald's own copy is in secrets.json
    either way, so a failure here never strands an account.
    """
    ext = config.get("apply.bitwarden_extension_id")
    if not ext:
        return False
    try:
        bw = context.new_page()
        bw.goto(f"chrome-extension://{ext}/popup/index.html#/add-cipher?type=1",
                wait_until="domcontentloaded")
        bw.wait_for_timeout(2500)
        bw.get_by_label("Item name", exact=False).first.fill(f"{company or host} (job applications)")
        bw.get_by_label("Username", exact=False).first.fill(email)
        bw.get_by_label("Password", exact=False).first.fill(pw)
        uri = bw.get_by_label("Website (URI)", exact=False)
        (uri if uri.count() else bw.get_by_label("URI", exact=False)).first.fill(f"https://{host}")
        bw.get_by_role("button", name="Save").first.click()
        bw.wait_for_timeout(2000)
        bw.close()
        return True
    except Exception:
        return False


def adapter_for(kind: str):
    return Workday if kind == "workday" else Generic
