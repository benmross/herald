# Memory revisions and source coverage

Conversation context combines a frozen startup card with substantive changes
appended to later messages. The current source records remain authoritative;
an update notice is a compact index into evidence, not a replacement for it.

## Revisions

`ledger_changes` records a monotonic `seq`, entity, stable record key, and
before/after JSON snapshots. SQLite triggers cover facts, commitments and
deadlines, including direct SQL edits and removals. Ingestion timestamps alone
do not create a revision. Source IDs identify records; they do not count changes.

For a conversation interval, changes to each record are combined into its
earliest previous state and final current state. Identical rebuilds and edits
that are reverted produce no notice. Changed body text still counts even when
the title is unchanged. Overflow points to the complete revision interval.

`db.replace_facts()` reconciles complete derived snapshots while retaining
unchanged rows. Gmail threads and labels and the Calendar list use it so quiet
refreshes do not produce thousands of redundant history entries. Existing
collectors using clear/rebuild remain supported; their net-identical records
are suppressed in notices. The change history is currently retained locally;
it contains source text and needs the same privacy protections as facts.db.

Conversation boundaries also scan Markdown under `identity/`, `state/` and
`journal/`. A persisted digest index avoids hashing unchanged files. The first
scan establishes a baseline; subsequent content edits and removals receive
revisions. The generated `state/orientation.md` is excluded. Documents outside
these maintained-memory directories are not scanned. Private sessions never
update this index or record revisions.

Existing topics with an old ID-based cursor receive a one-time upgrade notice.
Earlier changes cannot be reconstructed from that cursor, so the notice asks
the agent to check current records before relying on old context.

## Source health

`collector_state` separates these values:

| Field | Meaning |
|---|---|
| `last_run` | Last attempt |
| `last_ok` | Last run that returned, retained for existing cadence callers |
| `last_complete` | Last run reporting complete coverage within its scope |
| `sync_status` | unknown, complete, partial or failed |
| `pending_retries` | Unresolved items or listings reported by the collector |
| `coverage` | JSON describing the checked scope and counts |

Legacy `last_ok` timestamps do not establish completeness. The migration does
not manufacture historical `last_complete` values. Collectors report partial
coverage through `_sync` metadata; the runner retains successful records but
refuses to advance their cursor. A complete subsequent run clears the partial
status. `herald status`, cycle briefs, startup cards and conversation changes
surface incomplete coverage. A complete bounded window does not certify the
entire history of an account.

## Explicit reconciliation

```bash
herald collect gmail --reconcile
herald collect gcal --reconcile
```

These read the remote source and repair the local ledger. They do not change
mail or calendars on Google. Normal scheduled runs remain incremental.

Gmail lists messages from one day before the earliest retained message, or the
normal window on an empty ledger. It fetches missing messages and messages
changed in valid incremental history. If incremental history is unavailable,
it refreshes all messages in the explicit scope. It includes spam and trash.
Permanently deleted source content cannot be recovered.

Calendar lists every subscribed calendar across the normal window: 30 days
back and 500 days ahead. It refreshes records and removes stale local events
only within the scope of a successfully completed calendar listing. A failed
calendar or later page preserves absent local events and keeps the shared
cursor for retry. Older local history is preserved, not certified as current.

Network reads finish before either source takes the database writer lock.
Large reconciliation reads therefore do not lock conversations out of SQLite
while they download.

## Verification

`tests/test_memory_revisions.py`, `tests/test_delta.py` and
`tests/test_google_collector_cursors.py` contain incident-derived regressions
for in-place changes, deletion evidence, sparse IDs, unchanged rebuilds, document
edits, private mode, partial coverage, cursor recovery, and historical repair.
They use temporary or in-memory ledgers and fake services, with no model calls
or remote writes.

Personal evaluation questions and source-specific audit outcomes belong in the
private ledger. Evidence availability checks are distinct from model answer
accuracy; neither a passing schema test nor a successful sync proves that a
future model will retrieve and interpret the right evidence.
