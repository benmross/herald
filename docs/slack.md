# Slack

Herald reads Slack as you, through a Slack app you create in your own
workspace. A user token sees your channels and DMs; a bot token cannot see DMs.

1. api.slack.com/apps, Create New App, From a manifest, pick the workspace,
   paste `docs/slack-app-manifest.json`.
2. Install to Workspace. If the workspace requires approval, Slack sends the
   request to an admin. After approval, copy the User OAuth Token (`xoxp-`).
3. `herald secret request slack.tokens.<name> --why 'Slack user token'`, where
   `<name>` is a short label for the workspace. The token goes straight into
   `secrets.json` and never through chat.
4. `herald config set capabilities.slack true`, then `herald collect slack`.

`slack.backfill_days` (default 30) sets how far back the first run reaches.
Each run afterwards fetches only what is newer per conversation.
