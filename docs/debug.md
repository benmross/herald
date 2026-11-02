# Session observatory

`herald debug` serves a read-only dashboard on `127.0.0.1:8795`. It needs no
frontend build, CDN, browser account, or model call. Choose a session in the
sidebar to see its provider transcript, context measurements, turn timing,
commands and results. Search examines full event records, including content
outside the current page. Export downloads the complete selected source as
redacted JSONL. The dashboard paginates records, never truncates their text,
and includes archived Codex sessions and Claude subagent transcripts when
available. The overview covers all recorded runs, including failed runs that
have no session identifier.

## Private hosting

Run `herald config set debug.url '"https://your-machine.your-tailnet.ts.net:8450"'`,
then `herald services install`. After `herald check`, enable the optional
`herald-debug.service` user unit. Point Tailscale Serve at the loopback listener:

```sh
tailscale serve --bg --https=8450 http://127.0.0.1:8795
```

Use Serve, never Funnel. Anyone permitted to reach this service by your tailnet
policy can read the sessions; restrict that policy to your own devices if your
tailnet includes other people. The dashboard allows only its configured Host
and same-origin requests, sends no-store and restrictive CSP headers, executes
no content from transcripts, and provides no mutation or arbitrary-file API.

`debug.port` changes the local port. `debug.url` is unset by default and the
service is not enabled on a fresh install. The systemd template is optional;
on other platforms run `herald debug` under your preferred process manager.

## What is recorded

Existing transcripts are read in place, scoped to Herald sessions. The `runs`
and `run_phases` tables supply completed-turn metrics. They are opened read-only.
A missing transcript does not hide its run. The source selector distinguishes
provider transcripts, delegated Claude work and Herald captures, so replayed
stream events do not get counted twice in the same timeline.

New `think()` launches also write credential-redacted JSONL captures into
`$HERALD_HOME/debug-traces/` (directory mode 700, files mode 600). These include
the supplied prompt, appended system instructions, snapshots of the checkout's
CLAUDE.md and AGENTS.md when present, engine invocation, stdin,
stdout, stderr and outcome. Events carry UTC receipt timestamps to microsecond
precision and monotonic elapsed nanoseconds, ordered by sequence. Receipt time
is an observation, not an assertion about when the remote model executed a step.
Instruction snapshots describe files at launch, not proof of exactly what an
engine retained internally. The provider transcript remains the source for its
own recorded timestamps.
Captures become visible while a run is in progress; refresh updates the catalog.
A long-running surface must reload the updated code before its next run captures.

Private runs bypass capture completely and remain unlisted. Credentials known
to Herald, credential-shaped strings and structured credential fields are
redacted before capture and again on HTTP responses and exports. This is a
best-effort filter, not a guarantee that an unknown secret can be recognized.
Treat these files as personal data. They are outside the program and ledger
repositories. Set `debug.capture` to false to stop new captures; existing logs
are preserved. There is no automatic deletion or retention limit.

The engine can omit or truncate tool output, compact history, omit a timestamp,
or withhold internal reasoning. The dashboard cannot recover missing records
or reveal hidden reasoning. It displays available records verbatim apart from
credential redaction, and marks unknown timestamps and context values. Attached
binary media remains represented by its stored provider record, not fetched
from arbitrary local paths. Context charts show prompt size at each measured
model step, including cached input; they do not add prompts across a run. A
change of context window is retained in each measurement. Per-turn context is
available as a fallback when no step measurements exist.

Cost is the provider's client estimate for priced runs, not subscription billing.
Codex generally provides tokens without a dollar figure. Tool spans can overlap,
so phase totals are not an accounting of mutually exclusive wall time.

## Reading the activity and timing views

The default Activity view groups each call with its result and underlying
command records. Instructions are collapsed; exact repeats are marked. Provider
bookkeeping and mirrored message notifications remain in Raw records and JSONL
exports. Raw event numbers are retained for tracing a displayed activity back
to the source. Searches in Activity view also inspect the grouped original
records. Raw view searches every original record, including hidden metadata.

`session_meta` is a saved session header, which may include base instructions.
`task_started` marks an engine turn, not a shell task. `turn_context` saves
settings for a turn. The provider may serialize a message both as a conversation
item and as an item-completed notification. Herald captures save launch inputs
separately as well. These repeated representations do not establish extra
copies in the model's actual input; instructions can also be supplied
again on subsequent turns. Exact repeats are grouped only on matching content.

The session timing view uses timestamped request/result pairs. Parallel waits
are unioned, not summed. The remainder is labeled time between tool responses,
which includes model reasoning and writing, transport, overhead and background
work. It is not pure inference time. Calls with no recorded result appear as
unconfirmed waits. Missing timestamps do not become zero-duration measurements.
Idle gaps between turns are excluded. Provider turn boundaries are used when
available; inferred boundaries and incomplete turns are labeled partial.
Historical run-phase totals are still available but can overlap and are not
used as an exclusive wall-clock breakdown.

Context dots are input-size measurements, not tool events. Their tooltips show
recorded time, tokens, change from the previous measurement, context window when
known, and nearby activity. Temporal proximity does not prove causation. Dot
activation opens the linked activity with the correct pagination and turn.
The graph draws each turn separately and compresses inter-turn gaps with dashed
connectors. Turn headings and the turn selector focus the graph, timing lanes
and Activity timeline. Raw view continues to expose the whole selected source.

Claude's user-role records can be tool results, injected metadata or messages
from the person. Only authored text or attachments count as messages. Tool
results are paired with their requests and cannot open a turn. Mid-turn
messages remain in the active turn and are labeled steering updates. An
unblocked Claude stop-hook summary or a final result marks the response end;
a stop hook that prevents stopping does not close it. If completion markers
are missing, the view leaves the active turn unconfirmed instead of inventing
a new turn for every additional message.
