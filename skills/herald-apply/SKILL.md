---
name: herald-apply
description: Fill job and internship applications (Greenhouse, Ashby, Lever, SmartRecruiters, Workday) with `herald apply`, from the person's answer bank, creating accounts where their rules allow, and stopping before submit. Use whenever the user asks to apply, fill out applications, prepare applications for review, or continue applications already started; also to check which applications were submitted or rejected. Load it before opening any application form, and never submit one.
---

# Applications

`herald apply` does the form work in Python. A session's job is only to
answer questions the bank has never seen, all at once, and to write essays.
Every model round trip spent clicking a dropdown is waste.

**Never submit.** Nothing in `herald apply` presses a final submit, and you
must not either, by any route. The person reviews and submits every
application. Read the person's own rules for what else is allowed
(accounts, consents, demographics) in their constitution before starting.

## The loop

```bash
herald apply fill <url> [<url> ...] [--opp <opportunity id>]   # opens, fills, records
herald apply pending                                           # every unanswered question, one block
herald apply answer answers.json                               # store them, refill those applications
herald apply list                                              # status of everything
```

1. **Preflight, then fill in groups.** Check the exact requisition is open,
   fits the person's requirements, and is not already applied to. Pilot one
   application on each unfamiliar platform before expanding. `fill` allocates
   separate persistent workspaces, with at most ten applications each. Ten
   verified drafts trigger a protected group link automatically; a shorter
   final group is handed over when the batch finishes. While the person
   reviews one workspace, another session can fill a different workspace.
   Resource limits pause new work without discarding existing drafts.
2. **Answer everything pending in one `answer` call.** A JSON list:

   ```json
   [{"app": 3, "q": "What is your visa status?", "pattern": "visa status",
     "a": "No visa is required", "keep": "bank"},
    {"app": 7, "q": "Why Acme?", "a": "...", "keep": "company", "company": "Acme"},
    {"app": 7, "q": "Anything else we should know?", "a": null, "keep": "app"}]
   ```

   - `keep: bank` for anything true of the person everywhere (visa,
     relocation, years of experience). Give a `pattern` (a regex over the
     label) broad enough to catch the next wording of the same question.
   - `keep: company` for "why us" answers and essays: reused if that company
     appears again, never elsewhere.
   - `keep: app` for one-offs. `"a": null` means leave it blank on purpose.
   - For a choice, `a` is a regex or words matched against the options;
     a list ticks several.
3. The refill that `answer` runs reaches the end of the form or lists the
   next batch (multi-page Workday forms reveal questions page by page).
   Repeat until every line says ready for review.
4. Use the automatic group handoff or `herald apply review <ids...>`.
   Reissue a link with `herald apply review --group <group>`.
   Only current verified checkpoints qualify. The person switches between
   applications in the group selector, submits on each employer's page,
   and records that with “I submitted this”. Herald never submits.

## Checkpoints and ownership

`herald browse workspaces` shows ownership. Use `herald browse --workspace
<name> ...` for a specific worker. A human review or sign-in reservation
blocks automation until control is returned. Do not bypass it with raw CDP,
another Playwright connection, forced clicks, or by changing ownership rows.
Work on the same employer account is serialized, even in separate workspaces.

Filling writes append-only checkpoints before and after field changes and at
page boundaries, with semantic field identities and immutable attachment
copies. Each write must pass readback; ambiguous choices remain blockers.
Password, SSN, captcha and verification-code values are excluded in the page
before scanning. They must never enter the bank, logs or debug screenshots.
`herald apply snapshot` captures open drafts without filling or submitting.

After a browser restart, old review claims are invalidated. Return the group
for corrections, then refill and verify it before handing it over again.
Two identical failures stop automatic retries; use `refill --retry` only
after fixing the adapter or completing the access step. Never repeatedly
retry a rejected password. A checkpoint preserves what was observed; it
does not prove the website saved its own draft or preserve authentication.

## Writing essays

Read the posting once (`herald browse text <tab>`), then write in the
person's plain first person from the bank's profile and experience and
their resume. Facts only from those; never invent a project, a tool they
used or a number. A "why us" answer is about the company first. Store each
essay with `keep: company`, so it survives a lost tab and the next posting
at the same company.

## Accounts

The Workday adapter signs in with a stored account, or creates one (a new
password goes to secrets.json and, if `apply.bitwarden_extension_id` is
set, to the person's Bitwarden) and follows the verification email itself
(`herald apply code` reads the newest code or link). Only where the
person's rules say accounts may be created for them.

## Status

The `applications` collector marks rows submitted or rejected from
confirmation and rejection mail, hourly, and moves the linked opportunity
to applied or rejected. `herald apply mark <id> <status>` corrects one.

## When a form does not fill

- `herald apply refill <id>` again after fixing an answer; it skips what is
  already filled and reopens a lost tab from the bank.
- A field the adapter cannot drive (a custom widget): fill it with
  `herald browse` and say so in the review message.
- Something new about a site (an overlay, a widget, a different last page):
  fix it in `lib/herald/ats.py`, with the date and the site in a comment, so
  the next run does not rediscover it.
