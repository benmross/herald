# Asking questions from a session

`herald ask` sends a question to the Telegram topic that started the session.
Outside a Telegram turn it uses the Updates topic. You can answer with a
button or by replying to the question message.

```bash
herald ask 'Which export format do you prefer?' --option CSV --option JSON --no-wait
herald ask 'What should the new page be called?' --wait --timeout 600
herald ask --result 123
herald ask --result 123 --wait
```

Both ordinary questions and approval requests accept `--wait` and `--no-wait`.
With `--wait`, the command blocks and returns the answer as JSON. With
`--no-wait`, the default, it returns a pending question immediately. An answer
then enters the asking topic as a user message: it steers a live turn or starts
a follow-up turn if the session has finished. A blocking question returns its
answer through the waiting command and does not start a second turn.

The default expiry is one day. Use `--timeout` to change it, up to seven days.
An expired question cannot be answered. If delivery fails, the request expires
and the command reports the failure. Questions and answers survive a bridge
restart in the local database. Use the returned question id for later reads.

## Consent for an action without a handler

Write the complete proposal to a file, including the destination, exact public
text if applicable, and the scope of the action. Then request approval:

```bash
herald ask --approval --file /tmp/proposed-action.txt --wait --timeout 600
herald ask --approval --file /tmp/proposed-action.txt --no-wait
```

Approval requests always show the entire proposal as literal text. An oversized
proposal is refused before anything is sent. Their buttons are Approve and
Decline. Only an affirmative tap by the configured owner grants consent;
typing "yes" in a reply does not. Each button is bound to its question's chat,
topic and Telegram message. Repeated taps and expired requests do nothing.

An affirmative tap creates a consent receipt through `red.py`, with an entry
in the actions log. The receipt records permission, not execution. Check the
JSON result for `approved: true` before acting, then carry out only the action
described and record what happened in the journal. Decline and expiry never
authorize an action. The command returns a nonzero exit status for both.

Use dedicated action handlers where they exist. Generic consent supplies a
way to ask for permission when no handler exists; it does not change the tool
harness. A blocked command still needs the harness's own approval path, even
after a consent tap. The question mechanism neither executes shell commands
nor grants safety overrides.

Private sessions cannot send or store questions.
