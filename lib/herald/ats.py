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
import json
import re
from urllib.parse import urlparse

from . import apply as ap
from . import config, browser_workspaces as bw
from .application_checkpoints import PROTECTED_JS

# Profile keys every applicant has, by question label. The values live in the
# person's bank (apply.bank), never here.
PROFILE_RULES = [
    (r"^(legal )?first name|^given name", "first_name"),
    (r"^preferred (first )?name", "preferred_name"),
    (r"^(legal )?last name|^family name|^surname", "last_name"),
    (r"^(full |legal )?(first and last )?name$|^full legal name$|^legal first and last name", "full_name"),
    (r"^(confirm (your )?)?e-?mail", "email"),
    (r"^(mobile |cell )?phone( number)?$", "phone"),
    # Anchored: a question like "Are you proud of any GitHub repositories?"
    # got the bare profile URL on 23 Oct 2026 because "github" matched
    # anywhere. Only a field that asks for the profile link gets it.
    (r"^linkedin( profile| url| link)?$|^linkedin profile url", "linkedin"),
    (r"^github( profile| url| link| username)?( url)?$", "github"),
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
    label = re.sub(r"\s*\*+\s*$", "", label.strip())
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
    // Ashby's date box has only a placeholder ("Pick date..."); its question
    // is the field's own label (27 Oct 2026).
    if (!t && e.getAttribute('type') !== 'radio' && e.getAttribute('type') !== 'checkbox') t = questionOf(e);
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
  const protectedPattern = __PROTECTED__;
  const out = []; const groups = {};
  // Ashby yes/no fields use buttons; their backing checkbox loses the
  // distinction between No and unanswered (27 Oct 2026).
  document.querySelectorAll('.ashby-application-form-input-yesno').forEach((g, n) => {
    const wrap = g.closest('.ashby-application-form-field-entry');
    const lab = wrap && wrap.querySelector('label');
    const options = [...g.querySelectorAll('button[data-option]')].map((b, j) => {
      const hx = 'ashby-' + n + '-' + j;
      b.setAttribute('data-hx', hx);
      return {hx, label: txt(b), checked: b.getAttribute('aria-pressed') === 'true'};
    });
    out.push({kind: 'buttonradio', label: txt(lab), required: !!lab && /required/.test(lab.className), options});
  });
  // Oracle's select pills are ARIA radios without backing input elements
  // (27 Oct 2026). Include them so required answers cannot disappear.
  document.querySelectorAll('ul[role=radiogroup]').forEach((g, n) => {
    const buttons = [...g.querySelectorAll('button[role=radio]')];
    if (!buttons.length || !g.getBoundingClientRect().width) return;
    const row = g.closest('.input-row');
    const options = buttons.map((b, j) => {
      const hx = 'pill-' + n + '-' + j;
      b.setAttribute('data-hx', hx);
      return {hx, label: txt(b), checked: b.getAttribute('aria-checked') === 'true'};
    });
    out.push({kind: 'buttonradio', label: g.getAttribute('aria-label') || '',
      required: !!row && !!row.querySelector('.input-row__label--required'), options});
  });
  els.forEach((e, i) => {
    if (e.closest('.ashby-application-form-input-yesno')) return;
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
    const sensitive = type === 'password' || protectedPattern.test(labelOf(e)+' '+(e.id||'')+' '+(e.name||''));
    const o = {hx: i, kind, label: labelOf(e), required: req,
               field_id: e.id || '', name: e.name || '', protected: sensitive,
               value: kind === 'file' ? (e.files && e.files.length ? e.files[0].name : '')
                    : kind === 'password' ? (e.value ? '(set)' : '') : sensitive ? (e.value ? '(protected)' : '') : (e.value || '')};
    if (kind === 'select' && !sensitive) o.selected_label = e.selectedIndex >= 0 ? e.options[e.selectedIndex].text.trim() : '';
    if (kind === 'select') o.options = [...e.options].map(x => x.text.trim()).filter(x => x && !/^select/i.test(x));
    if (kind === 'combobox' && !sensitive) {
      const control = e.closest('[class*=control]');
      const selected = control && control.querySelector('[class*=singleValue],[class*=single-value],[class*=multiValue]');
      o.value = txt(selected) || e.value || '';
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
}""".replace('__PROTECTED__', PROTECTED_JS)


class Generic:
    name = "generic"

    def __init__(self, page, bank: dict, company: str | None, answers: dict, *, resume: str):
        self.page, self.bank, self.company, self.answers = page, bank, company, answers
        self.resume = resume

    def scan(self) -> list[dict]:
        from .application_checkpoints import safe_url
        found = []
        for fr in self.page.frames:
            try:
                counts = {}
                for f in fr.evaluate(SCAN_JS):
                    f["frame"] = fr
                    f['frame_path'] = safe_url(fr.url)
                    key = (f['label'], f['kind'])
                    f['occurrence'] = counts.get(key, 0)
                    counts[key] = f['occurrence'] + 1
                    found.append(f)
            except bw.Busy:
                raise
            except Exception:
                continue
        return found

    def loc(self, f, hx=None):
        return f["frame"].locator(f'[data-hx="{hx if hx is not None else f["hx"]}"]').first

    def options_of(self, f) -> list[str]:
        if f.get("options") and f["kind"] not in ("radio", "checks", "buttonradio"):
            return f["options"]
        if f["kind"] in ("radio", "checks", "buttonradio"):
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
        from .application_checkpoints import identity, protected
        if protected(f) and f['kind'] != 'file':
            return False
        # Resume parsing and dependent controls can rebuild the entire form.
        # Never reuse a numeric scan position against a different control.
        fresh = next((x for x in self.scan() if identity(x) == identity(f)), None)
        if fresh is None:
            return False
        f = fresh
        recorder = getattr(self, 'checkpoint', None)
        if recorder:
            recorder.before(f, value)
            if f['kind'] == 'file' and value is not None:
                recorder.upload(self.resume if value in (True, 'resume') else str(value),f)
        ok = self._set(f, value)
        return recorder.after(f, value, ok) if recorder else ok

    def _set(self, f, value) -> bool:
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
            except bw.Busy:
                raise
            except Exception:
                pass
            return True
        if k == "select":
            el = self.loc(f)
            if el.get_attribute("icimsdropdown-enabled") == "1":
                # iCIMS loads native select choices only after its visible
                # dropdown is opened (27 Oct 2026).
                fid = el.get_attribute("id")
                root = f["frame"]
                root.locator('[id=' + json.dumps(fid + '_icimsDropdown') + ']').click()
                box = root.locator('[id=' + json.dumps(fid + '_icimsDropdown_ctnr') + ']')
                self.page.wait_for_timeout(500)
                opts = box.locator('[role=option]:visible')
                labels = opts.all_text_contents()
                chosen = ap.best_option(value, labels)
                search = box.locator('input.dropdown-search:visible')
                if not chosen and search.count():
                    term = plain(value).split('|')[0].split(',')[0]
                    search.fill("")
                    search.type(term, delay=30)
                    self.page.wait_for_timeout(1200)
                    labels = opts.all_text_contents()
                    chosen = ap.best_option(value, labels)
                if chosen:
                    opts.nth(labels.index(chosen)).click()
                    self.page.wait_for_timeout(400)
                    return True
                root.locator('[id=' + json.dumps(fid + '_icimsDropdown') + ']').click()
                return False
            o = ap.best_option(value, f.get("options") or [])
            if not o:
                return False
            self.loc(f).select_option(label=o)
            return True
        if k in ("radio", "buttonradio"):
            o = ap.best_option(value, [x["label"] for x in f["options"]])
            hit = next((x for x in f["options"] if x["label"] == o), None)
            if not hit:
                return False
            if k == "buttonradio":
                self.loc(f, hit["hx"]).click()
            else:
                self._checked(f, hit["hx"], True)
            return True
        if k == "checks":
            wants = value if isinstance(value, list) else [value]
            if len(f["options"]) == 1 and isinstance(value, bool):
                self._checked(f, f["options"][0]["hx"], value)
                return True
            ok = False
            for w in wants:
                o = ap.best_option(w, [x["label"] for x in f["options"]])
                hit = next((x for x in f["options"] if x["label"] == o), None)
                if hit:
                    self._checked(f, hit["hx"], True)
                    ok = True
            return ok
        if k == "combobox":
            return self._combo(f, str(value))
        return False

    def _checked(self, f, hx, checked):
        el = self.loc(f, hx)
        if el.is_checked() == checked:
            return
        fid = el.get_attribute("id")
        label = f["frame"].locator('label[for=' + json.dumps(fid) + ']') if fid else None
        if label is not None and label.count() and label.first.is_visible():
            label.first.click()
        else:
            el.set_checked(checked, force=True)

    def _combo(self, f, want: str) -> bool:
        el = self.loc(f)
        el.click()
        self.page.wait_for_timeout(600)
        cid = el.get_attribute("aria-controls")
        root = f["frame"]
        scope = root.locator('[id=' + json.dumps(cid) + ']') if cid else root.locator("body")
        opts = scope.locator("[role=option]:visible, [role=gridcell]:visible")

        def pick():
            labels = [o.inner_text().strip() for o in opts.all()[:150]]
            o = ap.best_option(want, labels)
            if not o and re.search(r"location|\bcity\b", f.get("label", ""), re.I):
                # A place search returns "College Park, Maryland, United
                # States" for "College Park, Maryland": take the first
                # suggestion holding every word asked for (27 Oct 2026).
                words = set(re.findall(r"[a-z]+", plain(want).lower()))
                o = next((x for x in labels if words and words <= set(re.findall(r"[a-z]+", x.lower()))), None)
            if o:
                opts.nth(labels.index(o)).dispatch_event("click")
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
        except bw.Busy:
            raise
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
        # iCIMS career pages link to a separate account/form host. This is
        # navigation into the form, never the form's submit control.
        links = self.page.get_by_role("link", name="Apply", exact=True)
        if links.count():
            target = links.first.get_attribute("href") or ""
            if "icims.com/jobs/" in target and "/login" in target:
                self.page.goto(target, wait_until="domcontentloaded")
                self.page.wait_for_timeout(4000)
        # Ashby overview pages have no form. Follow their Application link,
        # never the final submit button (observed 27 Oct 2026).
        if "jobs.ashbyhq.com" in urlparse(self.page.url).netloc and not self.page.url.rstrip("/").endswith("/application"):
            self.page.goto(self.page.url.rstrip("/") + "/application", wait_until="domcontentloaded")
            self.page.wait_for_timeout(4000)
        if not self.scan():
            return {"filled": [], "pending": [{"q": "Application form is not visible", "kind": "action"}],
                    "failed": [], "done": False}
        # Never upload twice. Greenhouse clears its file input once a file is
        # attached, so on a refill the first empty file input was the cover
        # letter's, and the resume went there (23 and 24 Oct 2026). If the
        # page already shows the resume's file name, it is attached.
        import os
        already = not bool(self.resume)
        for frame in self.page.frames if self.resume else []:
            try:
                already = already or frame.evaluate("n=>document.body.innerText.includes(n)||[...document.querySelectorAll('input[type=file]')].some(e=>[...e.files].some(f=>f.name===n))", os.path.basename(self.resume))
            except bw.Busy:
                raise
            except Exception:
                pass
        # Upload first, then read the form again: Ashby rebuilds the whole
        # form after a resume upload (it autofills from it), and on 23 Oct
        # 2026 every field found before the upload went stale and each one
        # waited out a 30-second timeout.
        for f in ([] if already else self.scan()):
            if f["kind"] != "file" or f.get("value"):
                continue
            label = (f.get("label") or "").strip()
            if re.search(r"cover|transcript", label, re.I):
                continue
            if not label or re.search(r"resume|cv|attach", label, re.I):
                try:
                    self.set(f, "resume")
                    filled.append("resume")
                    self.page.wait_for_timeout(3000)
                except bw.Busy:
                    raise
                except Exception as e:
                    failed.append(f"resume: {type(e).__name__}")
                break
        resume_done = True
        recorder = getattr(self,'checkpoint',None)
        if recorder:
            recorder.restore_uploads()
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
            already = (f.get("value") not in (None, "")) if k not in ("radio", "checks", "buttonradio") else \
                any(o["checked"] for o in f["options"])
            from .application_checkpoints import matches, protected
            if protected(f):
                if f.get('required') and not already:
                    pending.append({'q': 'Complete the protected field yourself: '+label, 'kind': 'action', 'required': True})
                continue
            recorder = getattr(self, 'checkpoint', None)
            value, found = recorder.saved_value(f) if recorder else (None, False)
            if not found:
                value, found = resolve(self.bank, label, self.company, self.answers)
            if already and (not found or matches(f, value)):
                continue
            if not found:
                if f.get("required") or k == "textarea":
                    pending.append(self._pending(f, label))
                continue
            if k in ("radio", "checks", "buttonradio", "select") and not f.get("required") and value is not None:
                # An optional choice whose stored answer names none of its
                # options was found by a borrowed label: Ashby's SMS-consent
                # box reads as "Mobile Number" and was sent the phone number
                # (27 Oct 2026). Leave it alone.
                opts = [o["label"] if isinstance(o, dict) else o for o in f.get("options") or []]
                wants = value if isinstance(value, list) else [value]
                if not isinstance(value, bool) and not any(ap.best_option(w, opts) for w in wants):
                    continue
            try:
                ok = self.set(f, value)
            except bw.Busy:
                raise
            except Exception as e:
                ok = False
                failed.append(f"{label}: {type(e).__name__}")
            if ok:
                filled.append(label)
            elif f.get("required"):
                pending.append(self._pending(f, label, note=f"stored answer {value!r} matched no option"))
        final = self.page.get_by_role("button", name=re.compile(r"^Submit( Application)?$|^Send Application$", re.I))
        if not final.count():
            pending.append({"q": "Final application form is not visible", "kind": "action"})
        return {"filled": filled, "pending": pending, "failed": failed, "done": bool(final.count())}

    def _pending(self, f, label, note=None) -> dict:
        p = {"q": label, "kind": f["kind"], "required": bool(f.get("required"))}
        try:
            opts = self.options_of(f)
        except bw.Busy:
            raise
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
  const protectedPattern = __PROTECTED__;
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
    const sensitive = kind === 'password' || protectedPattern.test(lab+' '+id);
    if (sensitive) value = value ? '(protected)' : '';
    f.setAttribute('data-wd-i', i);
    const occurrence = out.filter(x => x.id === id).length;
    const selected_labels = [...f.querySelectorAll('input[type=radio]:checked,input[type=checkbox]:checked')].map(r=>((f.querySelector('label[for="'+r.id+'"]')||{}).innerText||'').trim());
    out.push({i, id, occurrence, label: lab, kind, value, options, required: req, protected: sensitive, selected_labels});
  });
  return out;
}""".replace('__PROTECTED__', PROTECTED_JS)

# Workday field ids that map straight onto the profile.
WD_IDS = {
    "legalName--firstName": "first_name", "legalName--lastName": "last_name",
    "addressLine1": "address1", "city": "city", "postalCode": "postal",
    "phoneNumber": "phone_digits", "countryRegion": "state", "phoneType": "phone_type",
    "school": "school", "degree": "degree_workday", "fieldOfStudy": "major",
    "schoolName": "school",
    "firstYearAttended": "start_year", "lastYearAttended": "end_year",
    "gradeAverage": "gpa",
}


class Workday:
    carries_writes = True
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
        # Country changes rebuild Workday fields and move their scan indices.
        # Tenant field ids survive the rebuild (DIRECTV, 27 Oct 2026).
        return self.page.locator('[data-automation-id=' + json.dumps('formField-' + f['id']) + ']').nth(f.get('occurrence', 0))

    def click(self, loc):
        # Workday lays a transparent click_filter div over its buttons, so a
        # normal click waits forever for the button to be hittable. Fall back
        # to a mouse click at the button's centre, which lands on the filter.
        recorder = getattr(self, 'checkpoint', None)
        if recorder:
            recorder.check_control()
        loc.scroll_into_view_if_needed()
        try:
            loc.click(timeout=4000)
        except bw.Busy:
            raise
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
            button = b.locator('button[aria-haspopup="listbox"]').first
            button.dispatch_event("click")
            self.page.wait_for_timeout(600)
            if not self.page.locator('[role=option]:not([data-automation-id="selectedItem"]):visible').count():
                button.dispatch_event("click")
                self.page.wait_for_timeout(400)
            o = [x.inner_text().strip() for x in self.page.locator('[role=option]:not([data-automation-id="selectedItem"]):visible').all()]
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
        from .application_checkpoints import identity, protected
        if protected(f):
            return False
        fresh = next((x for x in self.scan() if identity(x) == identity(f)), None)
        if fresh is None:
            return False
        f = fresh
        recorder = getattr(self, 'checkpoint', None)
        if recorder:
            recorder.before(f, value)
            if f['kind'] == 'file' and value is not None:
                recorder.upload(self.resume,f)
        ok = self._set(f, value)
        return recorder.after(f, value, ok) if recorder else ok

    def _set(self, f, value) -> bool:
        if value is None:
            return True
        b, k, p = self.box(f), f["kind"], self.page
        if k in ("dropdown", "prompt") and f.get("value") and ap.best_option(value, [f["value"]]):
            return True
        if k == "dropdown":
            button = b.locator('button[aria-haspopup="listbox"]').first
            button.dispatch_event("click")
            p.wait_for_timeout(600)
            opts = p.locator('[role=option]:not([data-automation-id="selectedItem"]):visible')
            if not opts.count():
                button.dispatch_event("click")
                p.wait_for_timeout(400)
            labels = [x.inner_text().strip() for x in opts.all()]
            o = ap.best_option(value, [x for x in labels if not re.match(r"^select one$", x, re.I)])
            if not o:
                p.keyboard.press("Escape")
                return False
            opts.nth(labels.index(o)).dispatch_event("click")
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
                    o = next((x for x in labels if re.fullmatch(r"other", x, re.I)), None) if f["id"] == "source" else None
                if o is None:
                    break
                opts.nth(labels.index(o)).dispatch_event("click")
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
            # Workday tracks the active segment through its display, not the
            # hidden input's focus. Filling inputs directly can put the year
            # into the day (F5's three-part date, 27 Oct 2026).
            parts = re.findall(r"\d+", str(value))
            p.bring_to_front()
            inputs = b.locator('input[role="spinbutton"]').all()
            if not inputs:
                inputs = b.locator("input:visible").all()
            for inp, part in zip(inputs, parts):
                segment = inp.get_attribute("aria-label")
                display = b.locator('[data-automation-id=' + json.dumps('dateSection' + (segment or '') + '-display') + ']')
                if display.count():
                    display.click()
                    p.keyboard.press("Control+a")
                    p.keyboard.type(part, delay=40)
                    p.keyboard.press("Tab")
                else:
                    inp.fill(str(int(part)), force=True)
                    inp.dispatch_event("blur")
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
        recorder = getattr(self, 'checkpoint', None)
        if recorder:
            v, found = recorder.saved_value(f)
            if found:
                return v, found
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
            return self.sign_in(acct)
        return self.create_account(acct, email, pw_fields)

    def signed_out(self) -> bool:
        """An expired Workday session still shows the form, empty and with a
        Sign In button in the header; the saved draft only appears after
        signing in (27 Oct 2026: DIRECTV and F5 filled from blank)."""
        p = self.page
        try:
            head = p.locator('[data-automation-id="utilityButtonSignIn"]:visible')
            if head.count():
                return True
            return bool(p.get_by_role("button", name=re.compile(r"^Sign In$")).count()
                        and not p.locator("input[type=password]:visible").count())
        except bw.Busy:
            raise
        except Exception:
            return False

    def dismiss_cookies(self):
        p = self.page
        for name in (r"^Accept Cookies$", r"^Accept All( Cookies)?$", r"^Accept$", r"^I Accept$"):
            b = p.get_by_role("button", name=re.compile(name, re.I))
            if b.count() and b.first.is_visible():
                try:
                    b.first.click(timeout=3000)
                    p.wait_for_timeout(800)
                except bw.Busy:
                    raise
                except Exception:
                    pass
                return

    def sign_in(self, acct) -> str:
        p = self.page
        # Go to the tenant's own login page rather than toggling the
        # create-account form: on 24 Oct 2026 the toggle left the email in
        # the wrong box and the sign-in failed. The redirect brings the
        # browser back to this application afterwards.
        from urllib.parse import quote, urlparse
        u = urlparse(getattr(self, 'application_url', p.url))
        parts = [x for x in u.path.split("/") if x]
        site = "/".join(parts[:2]) if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]) else parts[0]
        back = u.path if "/apply" in u.path else u.path.rstrip("/") + "/apply/applyManually"
        p.goto(f"{u.scheme}://{u.netloc}/{site}/login?redirect={quote(back, safe='')}",
               wait_until="domcontentloaded")
        try:
            p.locator('input[data-automation-id="password"]').first.wait_for(state="visible", timeout=15000)
        except bw.Busy:
            raise
        except Exception:
            return "sign-in failed: the sign-in page did not load"
        # A cookie banner over the button swallowed the click on ICF
        # (27 Oct 2026), and the unchanged page read as a rejected password.
        self.dismiss_cookies()
        # Focus each box before filling it, and press the button with the
        # mouse at its centre. On ICF (27 Oct 2026) a locator click landed on
        # Workday's click filter, nothing was submitted, and the form reset;
        # the same steps by mouse signed in at once.
        for sel, value in (('input[data-automation-id="email"]', acct["email"]),
                           ('input[data-automation-id="password"]', acct["password"])):
            box = p.locator(sel).first
            box.click()
            box.fill(value)
        btn = p.locator('[data-automation-id="signInSubmitButton"]')
        btn = btn.first if btn.count() else p.get_by_role("button", name="Sign In").first
        btn.scroll_into_view_if_needed()
        bb = btn.bounding_box()
        p.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
        try:
            p.locator('input[data-automation-id="password"]').first.wait_for(state="hidden", timeout=20000)
        except bw.Busy:
            raise
        except Exception:
            err = p.locator('[data-automation-id="errorMessage"]:visible, [role=alert]:visible')
            said = err.first.inner_text()[:160] if err.count() else ""
            if re.search(r"password|credential|invalid|incorrect|locked", said, re.I):
                return "sign-in failed: the stored password was not accepted"
            return "sign-in failed: the page did not respond" + (f" ({said})" if said else "")
        p.wait_for_timeout(3000)
        return "signed in"

    def create_account(self, acct, email, pw_fields) -> str:
        p = self.page
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
        # Some tenants use schoolName rather than school. Counting the wrong
        # id made each refill append another education (27 Oct 2026).
        if "school" in fields and any(f["id"] == "schoolName" for f in self.scan()):
            fields = {("schoolName" if k == "school" else k): v for k, v in fields.items()}
        first_id = next(iter(fields))
        existing = sum(f["id"] == first_id for f in self.scan())
        added = 0
        for index, item in enumerate(items):
            if index < existing:
                fs = [f for f in self.scan() if f["id"] in fields and f.get("occurrence", 0) == index]
                for fid, key in fields.items():
                    f = next((x for x in fs if x["id"] == fid), None)
                    if f and item.get(key) not in (None, ""):
                        self.set(f, item[key])
                continue
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
            fs = [f for f in self.scan() if f["id"] in fields and f.get("occurrence", 0) == index]
            for fid, key in fields.items():
                f = next((x for x in fs if x["id"] == fid), None)
                if f and item.get(key) not in (None, ""):
                    try:
                        self.set(f, item[key])
                    except bw.Busy:
                        raise
                    except Exception:
                        raise
            added += 1
        return added

    def fill(self) -> dict:
        p = self.page
        filled, pending, failed = [], [], []
        # From the posting: Apply, then Apply Manually (predictable, unlike
        # "Autofill with Resume", which guesses at work history).
        start = p.locator('[data-automation-id="adventureButton"]:visible')
        if not start.count():
            # A posting with a saved draft offers "Continue Application"
            # instead of Apply (ICF, 27 Oct 2026); it opens the draft.
            start = p.get_by_role("link", name=re.compile(r"^Continue Application$", re.I))
            if not start.count():
                start = p.get_by_role("button", name=re.compile(r"^Continue Application$", re.I))
        if start.count() and not p.locator('[data-automation-id^="formField-"]').count():
            self.click(start.first)
            p.wait_for_timeout(2500)
        # The chooser can also be the whole page (a reload of /apply lands
        # there), so look for it whether or not Apply was pressed.
        if not p.locator('[data-automation-id^="formField-"]').count():
            manual = p.locator('[data-automation-id="applyManually"]:visible')
            if not manual.count():
                manual = p.get_by_role("button", name=re.compile(r"^Apply Manually$", re.I))
            if manual.count():
                self.click(manual.first)
                p.wait_for_timeout(4000)
        acct = self.account_step()
        if not acct and self.signed_out() and (ap.account(self.host) or {}).get("created", True) is True \
                and ap.account(self.host):
            acct = self.sign_in(ap.account(self.host))
            if acct == "signed in":
                # The redirect lands on the application; Workday offers the
                # saved draft there, possibly behind Apply / Apply Manually.
                p.wait_for_timeout(2000)
                for sel in ('[data-automation-id="adventureButton"]:visible', '[data-automation-id="applyManually"]:visible'):
                    b = p.locator(sel)
                    if b.count() and not p.locator('[data-automation-id^="formField-"]').count():
                        self.click(b.first)
                        p.wait_for_timeout(3000)
        if acct:
            filled.append(f"account: {acct}")
            if "waiting" in acct:
                return {"filled": filled, "pending": [{"q": "verify the account from your email", "kind": "action"}],
                        "failed": failed, "done": False}
            if 'password was not accepted' in acct:
                return {'filled': filled, 'pending': [{'q': 'Sign in yourself; the saved credentials were rejected. They will not be retried automatically.', 'kind': 'action'}], 'failed': ['Stored sign-in rejected'], 'done': False}
            if 'failed' in acct:
                return {'filled': filled, 'pending': [{'q': 'Sign-in did not complete: ' + acct.split(': ', 1)[-1], 'kind': 'action'}], 'failed': [], 'done': False}
        for _ in range(10):
            cur, total, name = self.step()
            if not total:
                break
            if not self.scan() and not SUBMITTY.search(p.locator('[data-automation-id="pageFooterNextButton"]').first.inner_text() if p.locator('[data-automation-id="pageFooterNextButton"]').count() else ''):
                try:
                    p.wait_for_function("() => [...document.querySelectorAll('[data-automation-id^=formField-]')].some(e=>e.getBoundingClientRect().width>0)", timeout=12000)
                except bw.Busy:
                    raise
                except Exception:
                    return {'filled': filled, 'pending': [{'q': 'The application page did not finish loading. The saved checkpoint is preserved.', 'kind': 'action'}], 'failed': ['Page controls unavailable'], 'done': False, 'step': name}
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
                self.entries("Education", self.bank.get("education", []),
                             {"school": "school", "degree": "degree", "fieldOfStudy": "major",
                              "firstYearAttended": "start_year", "lastYearAttended": "end_year",
                              "gradeAverage": "gpa"})
                if p.locator("input[type=file]").count() and "successfully uploaded" not in self.body().lower():
                    upload = next((f for f in self.scan() if f['kind']=='file'),None)
                    if upload and self.set(upload,'resume'):
                        p.wait_for_timeout(2000)
                        filled.append("resume")
            step_pending = []
            for f in self.scan():
                if f["value"] or f["kind"] == "file":
                    continue
                if f.get('protected'):
                    step_pending.append({'q': 'Complete the protected field yourself: '+f['label'], 'kind': 'action', 'required': True})
                    continue
                v, found = self.value_for(f)
                if not found:
                    if f["required"] or f["kind"] == "textarea":
                        q = {"q": f["label"], "kind": f["kind"], "required": f["required"], "step": name}
                        try:
                            o = self.options(f)
                            if o:
                                q["options"] = o[:40]
                        except bw.Busy:
                            raise
                        except Exception:
                            pass
                        step_pending.append(q)
                    continue
                try:
                    ok = self.set(f, v)
                except bw.Busy:
                    raise
                except Exception as e:
                    ok = False
                    failed.append(f"{f['label']}: {type(e).__name__}")
                (filled if ok else failed).append(f["label"] if ok else f"{f['label']}: no option for {v!r}")
                if not ok and f["required"]:
                    q = {"q": f["label"], "kind": f["kind"], "required": True, "step": name}
                    opts = self.options(f)
                    if opts:
                        q["options"] = opts
                    step_pending.append(q)
            pending += step_pending
            if step_pending:
                return {"filled": filled, "pending": pending, "failed": failed, "done": False, "step": name}
            nxt = p.locator('[data-automation-id="pageFooterNextButton"]')
            if not nxt.count():
                break
            label = nxt.first.inner_text().strip()
            if SUBMITTY.search(label):
                return {"filled": filled, "pending": pending, "failed": failed, "done": True, "step": name}
            recorder = getattr(self, 'checkpoint', None)
            if recorder:
                recorder.snapshot('before-next:'+name)
            self.click(nxt.first)
            p.wait_for_timeout(5000)
            nc, _, nn = self.step()
            if recorder:
                recorder.snapshot('page:'+nn)
            if nc == cur:
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
        vault_page = context.new_page()
        vault_page.goto(f"chrome-extension://{ext}/popup/index.html#/add-cipher?type=1",
                wait_until="domcontentloaded")
        vault_page.wait_for_timeout(2500)
        vault_page.get_by_label("Item name", exact=False).first.fill(f"{company or host} (job applications)")
        vault_page.get_by_label("Username", exact=False).first.fill(email)
        vault_page.get_by_label("Password", exact=False).first.fill(pw)
        uri = vault_page.get_by_label("Website (URI)", exact=False)
        (uri if uri.count() else vault_page.get_by_label("URI", exact=False)).first.fill(f"https://{host}")
        vault_page.get_by_role("button", name="Save").first.click()
        vault_page.wait_for_timeout(2000)
        vault_page.close()
        return True
    except bw.Busy:
        raise
    except Exception:
        return False


def adapter_for(kind: str):
    return Workday if kind == "workday" else Generic
