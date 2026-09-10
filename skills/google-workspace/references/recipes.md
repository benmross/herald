# Google API recipes

Working patterns for this setup. Every snippet assumes `from google_api import get_service`
and that the file is run through `scripts/grun`.

- [Cross-cutting](#cross-cutting)
- [Gmail](#gmail)
- [Calendar](#calendar)
- [Drive](#drive)
- [Sheets](#sheets)
- [Docs](#docs)
- [Tasks and Contacts](#tasks-and-contacts)

## Cross-cutting

**Pagination.** Most `list` calls return one page and a `nextPageToken`. Silently
processing only page one is the most common bug in this whole area — it looks like it
worked and quietly misses data.

```python
def paginate(resource, method="list", key="items", **kwargs):
    """Yield every item across all pages of a list endpoint."""
    request = getattr(resource, method)(**kwargs)
    while request is not None:
        response = request.execute()
        yield from response.get(key, [])
        request = getattr(resource, f"{method}_next")(request, response)
```

`list_next` exists on most collections and handles the token for you. Where it doesn't,
loop on `pageToken=response.get("nextPageToken")` until the key is absent.

**Ask for fewer fields.** The `fields` parameter cuts response size and latency a lot:

```python
service.files().list(fields="files(id,name,mimeType,modifiedTime),nextPageToken")
```

**Retries.** Transient 403 rate-limit and 5xx errors are normal at volume. Wrap bulk
work in exponential backoff rather than letting one blip kill a long job:

```python
import random, time
from googleapiclient.errors import HttpError

def with_retry(call, attempts=5):
    for attempt in range(attempts):
        try:
            return call()
        except HttpError as exc:
            if exc.resp.status not in (403, 429, 500, 502, 503) or attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt + random.random())
```

## Gmail

### Search

`q` takes the same query language as the Gmail search box, which is far more precise
than filtering client-side: `from:sarah@x.com`, `subject:invoice`, `has:attachment`,
`is:unread`, `label:receipts`, `after:2026/01/01`, `newer_than:7d`, `-in:chats`.

```python
gmail = get_service("gmail", "v1")
msgs = list(paginate(gmail.users().messages(), key="messages",
                     userId="me", q="is:unread from:someone@example.edu"))
```

`messages.list` returns only ids — fetch each message for content.

### Reading a body

Bodies are base64url-encoded and nested in a MIME tree, so a plain `["body"]["data"]`
lookup returns nothing on any multipart mail. Walk the parts:

```python
import base64

def walk_parts(payload):
    yield payload
    for part in payload.get("parts", []) or []:
        yield from walk_parts(part)

def get_body(message, prefer="text/plain"):
    parts = list(walk_parts(message["payload"]))
    for mime in (prefer, "text/html"):
        for part in parts:
            data = part.get("body", {}).get("data")
            if part.get("mimeType") == mime and data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    return ""

full = gmail.users().messages().get(userId="me", id=msg_id, format="full").execute()
text = get_body(full)
```

Use `format="metadata"` with `metadataHeaders=["From","Subject","Date"]` when you only
need headers — it is much lighter than pulling full bodies.

### Sending, and replying in-thread

A reply that doesn't set `threadId` plus the `In-Reply-To`/`References` headers shows up
as a new conversation, which looks broken to the recipient.

```python
from email.message import EmailMessage

def send(gmail, to, subject, body, reply_to_message=None):
    mail = EmailMessage()
    mail["To"] = to
    mail["Subject"] = subject
    mail.set_content(body)

    payload = {}
    if reply_to_message:
        headers = {h["name"].lower(): h["value"]
                   for h in reply_to_message["payload"]["headers"]}
        message_id = headers.get("message-id", "")
        mail["In-Reply-To"] = message_id
        mail["References"] = f"{headers.get('references', '')} {message_id}".strip()
        payload["threadId"] = reply_to_message["threadId"]

    payload["raw"] = base64.urlsafe_b64encode(mail.as_bytes()).decode()
    return gmail.users().messages().send(userId="me", body=payload).execute()
```

Attach files with `mail.add_attachment(data, maintype=..., subtype=..., filename=...)`.

**Drafting is the safe default** when the user hasn't clearly said "send it" —
`users().drafts().create(userId="me", body={"message": payload})` puts it in front of
them to approve.

### Labels and cleanup

```python
gmail.users().messages().batchModify(userId="me", body={
    "ids": ids[:1000],                    # batchModify caps at 1000 ids per call
    "addLabelIds": ["Label_123"],
    "removeLabelIds": ["UNREAD", "INBOX"],
}).execute()
```

Removing `INBOX` archives; it does not delete. `messages().trash()` is recoverable for
30 days, `messages().delete()` is permanent and immediate. Get label ids from
`users().labels().list()` — the API wants ids, not display names.

## Calendar

### Reading a day or a range

```python
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

eastern = ZoneInfo("America/New_York")
start = datetime.now(eastern).replace(hour=0, minute=0, second=0, microsecond=0)

events = get_service("calendar", "v3").events().list(
    calendarId="primary",
    timeMin=start.isoformat(),
    timeMax=(start + timedelta(days=1)).isoformat(),
    singleEvents=True,      # expands recurring series into individual instances
    orderBy="startTime",    # only permitted when singleEvents is True
).execute().get("items", [])
```

Without `singleEvents=True` a weekly class shows up once as a rule, not as the
occurrences the user actually wants to see.

All-day events use `start.date`; timed events use `start.dateTime`. Handle both:
`event["start"].get("dateTime") or event["start"]["date"]`.

### Writing to a specific calendar

Herald's config says which calendar each kind of event belongs on
(`calendars.write`). Resolve the name to an id once:

```python
cal = get_service("calendar", "v3")
target = next(c["id"] for c in cal.calendarList().list().execute()["items"]
              if c["summary"] == "Classes")
```

Within Herald itself, do not call `insert` directly — go through
`lib/herald/gwrite.py`, which logs the write so the next digest can report it.

### Creating events, including recurring classes

```python
cal.events().insert(calendarId=target, body={
    "summary": "Algorithms lecture",
    "location": "Iribe Center for Computer Science, 8125 Paint Branch Dr, "
                "College Park, MD 20742, Room 0324",
    "start": {"dateTime": "2026-09-08T10:00:00", "timeZone": "America/New_York"},
    "end":   {"dateTime": "2026-09-08T10:50:00", "timeZone": "America/New_York"},
    "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL=20261211T235959Z"],
}).execute()
```

Notes that bite:
- `UNTIL` must be UTC with a trailing `Z`, even when the event is in Eastern time.
- Exclude breaks with an `EXDATE` line alongside the `RRULE`, one entry per skipped
  occurrence: `"EXDATE;TZID=America/New_York:20261125T100000"`.
- To change one occurrence, list instances with `events().instances()` and patch the
  specific instance id — patching the series id changes every occurrence.

### Free/busy

```python
cal.freebusy().query(body={
    "timeMin": start.isoformat(), "timeMax": end.isoformat(),
    "items": [{"id": "primary"}, {"id": target}],
}).execute()["calendars"]
```

## Drive

### Search

```python
drive = get_service("drive", "v3")
files = list(paginate(drive.files(), key="files",
    q="name contains 'budget' and mimeType='application/vnd.google-apps.spreadsheet' "
      "and trashed=false",
    fields="files(id,name,modifiedTime,webViewLink),nextPageToken",
    orderBy="modifiedTime desc"))
```

`trashed=false` matters — trashed files come back otherwise. Escape apostrophes in
user-supplied names (`name.replace("'", r"\'")`). For shared drives add
`includeItemsFromAllDrives=True, supportsAllDrives=True`.

### Upload and download

```python
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

drive.files().create(
    body={"name": "report.pdf", "parents": [folder_id]},
    media_body=MediaFileUpload("report.pdf", resumable=True),
    fields="id,webViewLink",
).execute()
```

Native Google formats (Docs/Sheets/Slides) cannot be downloaded with `get_media` —
they must be exported:

```python
import io
request = drive.files().export_media(fileId=doc_id, mimeType="application/pdf")
buf = io.BytesIO()
downloader = MediaIoBaseDownload(buf, request)
while not (done := downloader.next_chunk()[1]):
    pass
```

Use `files().get_media(fileId=...)` for binary files that were uploaded as-is.

**Sharing changes who can see the user's data.** `permissions().create()` is exactly
the kind of externally-visible write to confirm before running, especially with
`role="writer"` or `type="anyone"`.

## Sheets

```python
sheets = get_service("sheets", "v4").spreadsheets()

rows = sheets.values().get(
    spreadsheetId=sid, range="Sheet1!A1:F",
).execute().get("values", [])
```

Ragged rows are the trap: `values` omits trailing empty cells, so rows come back with
different lengths and indexing past the end raises `IndexError`. Normalize first:

```python
width = max(len(r) for r in rows)
rows = [r + [""] * (width - len(r)) for r in rows]
```

Writing:

```python
sheets.values().update(
    spreadsheetId=sid, range="Sheet1!A1",
    valueInputOption="USER_ENTERED",   # parses dates/formulas as if typed
    body={"values": [["Name", "Total"], ["Ada", "=SUM(B2:B10)"]]},
).execute()
```

`RAW` stores strings literally — use it when a value must not be reinterpreted.
`values().append()` adds to the end; `batchUpdate()` handles formatting, column widths,
and sheet structure.

## Docs

Docs edits are index-based, and every insertion shifts the indices after it. Apply
multiple edits in one `batchUpdate` ordered **back to front** so earlier offsets stay
valid:

```python
docs = get_service("docs", "v1").documents()
docs.batchUpdate(documentId=doc_id, body={"requests": [
    {"insertText": {"location": {"index": 120}, "text": "Later edit\n"}},
    {"insertText": {"location": {"index": 1},   "text": "Earlier edit\n"}},
]}).execute()
```

Read structure with `docs.get(documentId=...)`, then walk
`body.content[].paragraph.elements[].textRun.content`. For a plain-text dump, exporting
via Drive is usually less work than walking the tree.

## Tasks and Contacts

```python
tasks = get_service("tasks", "v1")
lists = tasks.tasklists().list().execute()["items"]
tasks.tasks().insert(tasklist=lists[0]["id"], body={
    "title": "Submit registration form",
    "due": "2026-09-15T00:00:00.000Z",   # date-only in practice; time is ignored
}).execute()

people = get_service("people", "v1")
contacts = people.people().connections().list(
    resourceName="people/me",
    personFields="names,emailAddresses,phoneNumbers",
    pageSize=1000,
).execute().get("connections", [])
```

People API requires `personFields` on every read and returns nothing useful without it.
