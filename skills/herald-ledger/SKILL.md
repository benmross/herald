---
name: herald-ledger
description: Query what Herald knows about the person it works for — deadlines and due dates, every calendar and event, recent mail and which threads await a reply, messages and who is waiting on a reply, tasks, contacts, where they have been, GitHub activity, and tracked opportunities with their deadlines. Use this for any question about their schedule, deadlines, inbox, messages, commitments, whereabouts, opportunities, or what they have been working on, and before answering anything about "this week", "what's due", "who am I forgetting", "did I reply to", or "where was I". Also covers Herald's own spend and collector health.
---

# The ledger

Everything Herald has ingested lives in one SQLite database. Query it; do not
read files into context. A query returns twenty rows — the corpus is thirty
thousand.

```bash
herald db --schema              # what is in there and how to reach it
herald db "select ..."          # table output
herald db "select ..." --json   # parseable
```

**Start with `herald db --schema` if you are unsure of a source name or a
column.** It is one call, it is authoritative, and it reflects *this* install —
which extensions add sources the program itself has never heard of. Guessing
costs far more: a session once spent three minutes on "what did I last text my
mom". It guessed the sources `imessage`, `sms` and `messages` (none of which
existed there), tried the `sqlite3` CLI (not installed on that machine), spent
131 seconds on a filesystem search for it, then grepped through
`lib/herald/*.py` to reconstruct the columns. The answer was one query.

**There may be no `sqlite3` binary.** Use `herald db`, or Python's `sqlite3`
module. Do not go looking for the CLI.

**Reach into `data` with `json_extract(data, '$.field')`**, not with `LIKE` over
the JSON text.

Ingestion runs on a timer, each collector at its own cadence. If something looks
stale, check `herald status` before assuming the data is wrong.

## Shape

One wide `facts` table carries every source, because the sources are
heterogeneous and a table per source would mean a migration per source.

| column | meaning |
|---|---|
| `source` | which collector wrote it — `gmail`, `gcal`, and whatever the extensions add |
| `kind` | what it is within that source (below) |
| `external_id` | the source's own id — stable across runs |
| `ts` | ISO8601, when the thing happens or happened |
| `title` / `body` | the human-readable parts |
| `data` | JSON; reach into it with `json_extract(data, '$.field')` |
| `ingested_at` | when Herald last saw it |

Timestamps are in the configured timezone as ISO strings, so `substr(ts,1,10)`
is the date and plain string comparison against `date('now')` works. An all-day
item carries a bare date and `data.all_day = 1`, which means "due that day", not
midnight.

### The built-in sources

| source/kind | notes |
|---|---|
| `gmail/message` | recent mail, metadata + snippet | `data.from`, `data.bulk`, `data.unread`, `data.labels` |
| `gmail/thread` | derived — `data.awaiting_reply` is the one that matters |
| `gmail/label` | label id → name; join on `data.labels` in messages |
| `gcal/event` | a window around today, recurrence expanded. **`data.role` first**, then `data.calendar`, `data.location`, `data.attendees` |
| `gcal/calendar` | the calendar list; `data.role`, and `data.writable` for where Herald may write |
| `gtasks/task` | all lists; `data.status` is `needsAction` or `completed` |
| `contacts/person` | address book; `data.emails` — join to put names on senders |
| `github/event`, `github/repo` | what they have actually been building |
| `<feed>/assignment` | anything with a due date, from a calendar feed. `data.tag` is the feed's own grouping (a course code, a project) |
| `<feed>/event`, `<feed>/tag` | the rest of a subscribed `.ics` feed |
| `opportunities/candidate` | ranked mail, raw material for the scout cycle, not conclusions |

### Shapes that mean the same thing whoever wrote them

The cycle snapshot queries these by `kind` alone, across every source, and so
should you:

| kind | meaning |
|---|---|
| `assignment` | something with a deadline |
| `thread` | a conversation; `data.awaiting_reply` says the last word was not theirs |
| `current` | where they are now; `data.age_minutes` |
| `day` | one row per day of somewhere they were |
| `health` | a source reporting on its own freshness — **check it before trusting that source**, `data.stale`, `data.hours_behind` |

Extensions add their own sources and document them in their own README or
skill. `herald db --schema` lists what this install actually has.

### The other tables

`opportunities` (things to apply for, with a lifecycle), `commitments` (open
loops), `runs` (every agent invocation and what it cost), `notifications`,
`collector_state`, `approvals`, and:

`actions` holds every write Herald made outside the ledger — `actor`, `kind`,
`target`, `summary`, `ref`, `reported_at`. "What did you change on my calendar?"
is `select * from actions order by ts desc`.

Bodies are snippets, not full mail. When you need a whole message, fetch it with
the `google-workspace` skill — do not expect it here.

## Recipes

**What is due, soonest first** — every source, not just one

```bash
herald db "select substr(ts,1,16) due, json_extract(data,'\$.tag') tag, title, source
           from facts where kind='assignment' and ts >= date('now')
           order by ts limit 15"
```

**Calendar provenance — read this before answering anything about their schedule
or what they are involved in.**

Every `gcal` event carries `data.role`, and it matters more than any other
field:

| role | what it means |
|---|---|
| `mine` | their actual commitments. Evidence about them. |
| `feed` | public events they *could* attend, put there by a machine. **Never evidence they are enrolled in, a member of, or committed to anything.** |
| `other-person` | someone else's calendar. Their commitments, not the user's. |
| `reference` | holidays, institutional dates. Context. |

Google reports a calendar the user's own scraper fills as `accessRole: owner`,
which is true — they do own it — and completely misleading. A session once
concluded someone was in a scholars programme from a single event on such a
calendar. Filter on `role`, from `calendars.roles` in the config, not on
`accessRole`.

**Today's schedule** — theirs, not the firehose

```bash
herald db "select substr(ts,12,5) at, title, json_extract(data,'\$.location') where_
           from facts where source='gcal' and kind='event'
             and json_extract(data,'\$.role')='mine'
             and substr(ts,1,10)=date('now') order by ts"
```

**Who is waiting on a reply** — `awaiting_reply` already excludes newsletters,
listservs and no-reply senders, so trust it as a starting point and apply
judgment on top.

```bash
herald db "select substr(ts,1,10) last, json_extract(data,'\$.last_from') who, title
           from facts where source='gmail' and kind='thread'
             and json_extract(data,'\$.awaiting_reply')=1 order by ts desc"
```

**Who is waiting on a message back**, from whatever message source exists here

```bash
herald db "select source, title, substr(ts,1,10) last, body
           from facts where kind='thread' and source<>'gmail'
             and json_extract(data,'\$.awaiting_reply')=1 order by ts desc"
```

Check freshness first — a source that knows it is behind says so, and "nobody is
waiting" from a sync that stopped two days ago is worse than saying nothing:

```bash
herald db "select source, title, json_extract(data,'\$.hours_behind') hours,
                  json_extract(data,'\$.stale') stale
           from facts where kind='health'"
```

**Put a name on a sender**

```bash
herald db "select p.title from facts p, json_each(p.data,'\$.emails') e
           where p.source='contacts' and e.value like '%someone@example.com%'"
```

**Where they were, day by day**

```bash
herald db "select ts day, title places, json_extract(data,'\$.first_seen') from_,
                  json_extract(data,'\$.last_seen') to_
           from facts where kind='day' order by ts desc limit 7"
```

**Where they are right now**

```bash
herald db "select title, json_extract(data,'\$.age_minutes') mins_ago
           from facts where kind='current'"
```

**Open commitments and live opportunities**

```bash
herald db "select id, due, kind, text from commitments where status='open' order by due"
herald db "select id, status, deadline, title, org, score from opportunities
           where status in ('new','surfaced','interested') order by coalesce(deadline,'9999')"
```

## Naming people

Message and contact rows carry the name as it appears in the user's address
book — a full name, not "Mom". `ledger/identity/` maps relationships to
names, so read it (including `identity/private/people.md` if it exists) before
answering anything phrased as "my mom" or "my cousin".

## Herald's own health

```bash
herald status                      # collectors, capabilities, spend, ledger size
herald db "select label, count(*) runs, round(sum(cost_usd),2) usd from runs
           where ts >= datetime('now','-7 days') group by label order by usd desc"
herald db "select collector, last_ok, consecutive_failures, last_error
           from collector_state order by collector"
```

A collector that is *off* is not a collector that is failing — `herald status`
distinguishes them, and a capability the user never connected has no business
being reported as broken.
