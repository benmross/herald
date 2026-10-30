# Compact context retrieval

`herald context` combines maintained topic notes with current structured status.
It uses deterministic file routing and SQL. It does not call a model, embed
content, search conversation history, or infer relationships from event listings.

## Topics and sections

```bash
herald context topics
herald context COURSE101 --budget 1500
herald context COURSE101 --sections
herald context COURSE101 --section 'Assessment policy'
herald context COURSE101 --section s120 --offset 10
herald context --file state/areas/COURSE101.md --section s120 --budget 0
```

The topic index comes from maintained area, project and reference filenames
and their first headings. Spaces and punctuation in names are normalized for
lookup. Ambiguous topics require an explicit file. Unknown topics can still
match structured records, but never acquire invented notes or membership.
There is no natural-language router or semantic search in this phase.

A topic packet contains the introductory note, short previews of up to two
current/status/contact sections, a partial heading index, status counts,
matching operational records and source freshness. Long notes are not returned
in full by default. An introductory excerpt cannot substitute for a
policy section or its original source. Source references and commands identify
how to obtain the remaining evidence.

Sections use ATX Markdown headings outside fenced code blocks. `s120` means the
heading currently begins on line 120. A parent section includes its child
sections through the next heading of the same or a lower level. Regenerate the
section index after a document changes rather than assuming line keys persist.
The section offset counts lines relative to its heading. Files must resolve to
Markdown inside the ledger; symlinks cannot escape that directory.

## Supported operational reads

```bash
herald context --view counts
herald context --view commitments
herald context COURSE101 --view deadlines --days 14
herald context --view schedule --days 2
herald context --view opportunities --offset 10 --limit 10
herald context --view applications
herald context --view applications --schema
herald context --view opportunities --id 12 --budget 0
```

Counts include all retained statuses and dates, while default record views use
the following scopes:

| View | Default scope |
|---|---|
| commitments | Open commitments, including overdue ones |
| deadlines | Live dates from today through the requested horizon |
| schedule | Personal events within local calendar dates, excluding feed and other-person calendars |
| opportunities | New, surfaced or interested records with absent or unexpired deadlines |
| applications | Recorded application statuses, including rejected or submitted rows |

`--all` includes historical statuses and dates. It still restricts schedule
results to personal events. `--id` explicitly retrieves one complete table row,
including detailed notes, subject to the output budget. Separate opportunity
and application statuses remain separate evidence: a saved draft is never
relabelled as a submission. No read changes lifecycle status.

Dates use the configured timezone, including all-day dates and events expressed
with another UTC offset. SQL values remain parameters. Topic matching is lexical
against the relevant fields, so an untagged obligation or paraphrased reference
can be missed. Empty results are not proof of absence, especially with incomplete
source coverage. Use the ordinary ledger tools for sources outside these views.

## Output limits and measurements

The default budget is 1,500 approximate tokens, measured as UTF-8 bytes divided
by four. This is an output-size heuristic rather than a vendor tokenizer. The
budget includes evidence, warnings and expansion pointers; a small retrieval
identifier is emitted separately on stderr. Use `--budget 0` for an unlimited
result, or page records and sections. Excerpts are explicit.

`retrievals` stores the category, elapsed time, output bytes, estimated tokens,
item count, whether more material exists and an optional parent retrieval. It
does not store query text, topic names, returned evidence or user messages.
`herald context stats` reports aggregate measurements without creating another
measurement for itself.

The engine launcher supplies an opaque per-invocation value. Its hash correlates
multiple context calls in that invocation. Calls after the first are linked as
additional lookups; this does not establish that the earlier result failed.
Outside that environment, or to record intent explicitly, pass
`--followup <retrieval id>`. Private context reads create no measurements and
write no files. A metrics failure does not prevent otherwise successful reads.

## Opportunity overview

Scout regenerates `state/opportunities.md` from structured records after a
successful pass. Its prompt keeps reasoning in fit/notes and history in the
journal. The generated file is a small dated overview with supported query
commands, counts and a few recorded deadlines. Tables remain authoritative.

Migration 0006 preserves any older narrative overview byte for byte under
`documents/archive/opportunities-<digest>.md` before replacement. Refreshes are
atomic and archives are retained. No application or opportunity status changes
during this migration. A clock change alone does not rewrite an unchanged
overview. Source-specific evaluation questions and measured
outcomes belong in the private ledger.
