# Changelog

What each release changed, from the commit messages. `herald update` shows the entry for whatever it is about to install.

## v0.4.2: 20 October 2026

- List the published extensions in the README
- Keep generated release headings consistent with the writing rules

## v0.4.1: 20 October 2026

- Warn before subscription limits leave upcoming work without capacity
- Route files and approvals into the General topic instead of Updates
- Pull requests go through a tap, and every tap lands in the topic that asked
- Add a Slack collector that reads the user's own workspaces with a user token
- Extensions say what setup needs, and the publish check insists on it
- Take the user's location from Telegram, and age fixes from their timestamp
- Give every enabled extension command a launcher in ~/.local/bin
- Ask the mode at commit time, and never tag a release whose commit failed

## v0.4.0: 18 October 2026

- Ask an extension's settings at install, and say how from Telegram
- Report a ledger backup that has stopped reaching its remote
- Install, update and vet extensions published as their own repos
- Drop fuzzy title matching; a model now owns near-duplicate decisions
- Match event titles by shared words, so near-identical names stop duplicating
- Print clock times in the user's format, set by time_format
- Take secrets from the phone through a one-time page, never through a model
- Route Telegram file sends to the topic that asked, and say that files can be sent
- Scout reads every message whole, with its pictures
- Add private sessions that can read everything and save or change nothing
- Give Sheets writes a logged door in gwrite
- Send what Claude says between tool calls as separate Telegram messages
- Show the terminal resume command in /debug and herald session
- Let each install choose its providers without a Claude dependency
- Close steering immediately when a user stops an active turn
- Let Telegram steer active Codex turns through the first-party app server
- Keep model and thinking selections in their original Telegram picker
- Offer model-specific thinking levels after Telegram model selection
- Expose both CLI model catalogs and per-topic selection in Telegram
- Keep Codex context when resuming with extra workspaces
- An extension can add a red action, behind the same tap

## v0.3.2: 7 October 2026

- The README leads with a quick start and says each thing once
- Herald runs in a Docker container, with a supervisor in place of systemd
- A fresh install ends on commands that work, and says so when nothing can be scheduled

## v0.3.1: 7 October 2026

- A reply to a message from an earlier conversation resumes that conversation
- The watchdog no longer reports a rule the user lifted as a broken guard
- Skills, cycle prompts and docs are rewritten without the patterns the prose check looks for
- The constitution is rewritten in the style it asks sessions to write in
- A reply is linted for machine-prose patterns as the turn ends, and rewritten once if it has them
- A reply given before background work lands is delivered, not left in the progress block
- herald find: locate, minus what has gone since the nightly index

## v0.3.0: 4 October 2026

- Safety is Herald's, not the engine's; root goes through a tap on a bot no session can reach
- Use a placeholder fact id in amber's refusal example
- An ignored calendar is ignored everywhere, not in one snapshot section
- Dates go on the calendar; the obligation list holds only open loops, and nothing enters it silently
- The progress block no longer repeats the reply underneath it
- Mail Herald writes carries an HTML part, so Gmail does not hard-wrap it
- Gmail drafts can carry attachments
- Sessions can write the ledger again
- A turn waits for its background tasks before it answers
- Cycles act on what they read, through a narrow amber door
- herald notify --file: a document arrives on the phone, not on the server

## v0.2.0: 22 September 2026

- Setup, doctor and the install docs know about the second engine
- The guard now reads a -c script that contains a semicolon
- Codex is a second engine, chosen per run, never a fallback
- Video goes as video, so it plays where it lands
- An image answers the topic that asked for it
- Herald can send images to the user, not just words
- Drive uploads can convert to a native Google file
- A message Telegram cut in half is one turn again
- A think ceiling of 0 means no absolute ceiling

## v0.1.9: 14 September 2026

- A quiz no longer eats the message that was not an answer
- Quizzes that answer without a model, and one turn per multi-file message
- Sessions answered from shortened copies, and paid for it in round trips
- The autonomy tiers were three colours over prose; make them structure
- A conversation could not see the world move underneath it
- Latency was never measured, so it was optimised by guesswork

## v0.1.8: 10 September 2026

- Setup reads like it was written for the person doing it, and does not lag
- The tracking hook decides at commit time, not at install time

## v0.1.7: 10 September 2026

- Three things the first outside install tripped on

## v0.1.6: 10 September 2026

- The installer checked venv support on the wrong interpreter

## v0.1.5: 10 September 2026

- Preflight tells WSL2 the truth about systemd
- The tracking guard no longer depends on one wizard screen having been seen
- A fresh install starts at a release, and the timezone guess actually runs
- The terminal wizard could not be left mid-step
- What a full rehearsal of a fresh install found

## v0.1.4: 10 September 2026

- Where to run it, and the login constraint that decides the answer

## v0.1.3: 10 September 2026

- A checkout is not an install, and now a test knows the difference

## v0.1.2: 10 September 2026

- Two bugs in the update path, both found by rehearsing one

## v0.1.1: 10 September 2026

- Nothing ever created the ledger symlink

## v0.1.0: 10 September 2026

- Two docstrings named the maintainer
- Three modes: follow upstream, own the program, or be upstream
- Progress: stop dropping text, and stop asking for more of it
- status: tell a retired collector apart from a broken one
- Telegram: a live transcript instead of status pings, and Markdown that renders
- Remove the authenticated Canvas reader
- Anchor the login needle on home directories, not on any slash
- Fix the check that failed the public repo's first CI run
- The last things that only made sense in one place
- Herald
