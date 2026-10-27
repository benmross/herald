# Answer buttons and action approvals

This is a design proposal for general conversation buttons. They are not
implemented yet. The existing red-action approval path and root bot remain
separate. Extension query commands cannot register callback handlers.

## Reuse the interface, keep the meaning explicit

Your questions and action approvals can share button rendering, delivery,
expiry and duplicate-tap handling. They need different record types and
handlers. A conversational answer adds information to a particular session.
An approval grants permission for a particular stored action.

For example, answering “Did you finish the report?” supplies a completion
statement for that report. It cannot grant permission to send the report to
someone. Sending it requires a separate approval displaying its recipient
and contents. Your existing green and amber actions do not acquire an extra
confirmation step. Sharing a button interface is not a reason to ask before
every private, reversible action.

The dispatcher should choose the meaning from a stored request type, never
from the text “yes,” a model's interpretation, or an extension-provided action
name. Question callbacks should have their own namespace and records, with
no route that changes the approvals table or calls a red performer.

## Bind each answer to its question

A proposed question record would hold its random token, owner, chat, topic,
message id, originating engine/session, complete question, fixed option
values and expiry. A callback would contain only an opaque token and option
index, not executable code, an arbitrary reply or an approval id. The bridge
would validate that the option belongs to that request and that the sender
and message match before accepting it.

Acceptance must atomically consume the pending record. Removing buttons is
useful feedback but is not replay protection. Repeated, expired, unknown or
wrong-owner taps should do nothing. A question from an earlier session must
return to that session, or be declared stale; it must not inject an ambiguous
“yes” into whichever conversation happens to be current.

The model should receive the complete question and selected answer together,
labelled as your answer. The quoted question remains context, not a new user
instruction. Private conversations need ephemeral records within their
existing private-session boundary, or should refuse this feature until that
exists. They must not silently use the ordinary persistent database.

Telegram supplies the sender and originating message separately from callback
data. Its API warns that callback data may refer to a message that no longer
contains such a button, so the stored request must remain authoritative.
See [Telegram's CallbackQuery reference](https://core.telegram.org/bots/api#callbackquery).

## Preserve action approvals

For a red action, the request must identify the exact operation, destination
and immutable payload to be performed. Show its complete relevant contents
before asking. After a tap, verify that the approved payload is still the one
being executed, consume approval once and record the result. A generic
question's answer cannot stand in for any of those checks. Typed answers such
as “yes” in the chat cannot replace the existing red-action tap either.

Current mail and pull-request approvals preview long descriptions. The issue
creation command displays the entire title and body as plain text and refuses
content that exceeds one message. A broader redesign should support complete
content review, including long payloads, rather than hiding their ends behind
an approval button.

## What the current checks protect

The normal non-root flow checks your Telegram user id, records a decision,
and lets the requesting process claim that decision exactly once. Those
checks catch duplicate taps, ordinary mistakes and requests that received no
approval. The guard also blocks recognizable attempts to bypass the logged
write paths. These controls do not make arbitrary code running as your Unix
user untrusted in an operating-system sense.

The same account can access the main bot credentials, write the approvals
database and modify the action code. A compromised session could change an
action's stored payload or state without a real tap. Random tokens, payload
hashes and separate tables help with mistakes and forged external callbacks;
they cannot secure state against a process that can rewrite all of them.
Enabled extensions are also trusted installed code. A query-only connection
catches accidental SQL writes, but does not sandbox Python.

Root is different: its daemon and bot credentials are owned by root, and the
daemon keeps the exact command in its own memory while waiting for your tap.
Editing the ordinary approvals database does not authorize a root command.
Keep that bot and execution path separate.

A stronger non-root boundary would need an isolated approval service under a
different account that owns the approval state, protects the credentials and
performs approved writes itself. Sessions would submit structured requests
and receive results, without an alternative path to those writes. Protecting
only the buttons while leaving write credentials with sessions would not
establish that boundary. This is a separate architecture change, not part of
adding convenient conversation buttons.
