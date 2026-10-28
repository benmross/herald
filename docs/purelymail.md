# Purelymail

Enable direct mailbox reading and drafts without Gmail forwarding. Purelymail's
administration API manages users and domains; it does not expose mailbox content.
This integration uses TLS IMAP at `imap.purelymail.com:993` with an app password.

Create an app password in the Purelymail account portal under your user, then:

```sh
herald config set purelymail.address '"you@example.org"'
herald secret request purelymail.password --why 'Direct mailbox access and drafts'
herald config set capabilities.purelymail true
herald ext enable purelymail
purelymail status
herald collect purelymail
```

Enter the app password in the secure form. Do not use an administration API key.
The credential is stored only in your private secrets file.

The collector reads selectable folders other than Drafts, Trash, Junk and Spam.
Its first pass covers 21 days and fetches at most 100 messages per folder per run.
Later passes resume using per-folder UIDVALIDITY and UID checkpoints. The settings
`purelymail.initial_days` and `purelymail.batch_per_folder` change those defaults.
A UIDVALIDITY change starts a new recent-window scan. Collection leaves read flags
unchanged. Complete MIME messages are cached privately for attachments and images.
Mail is available through `herald fact`, the briefing, and scout.

Create a draft in the account's existing Drafts folder:

```sh
purelymail draft --to recipient@example.org --subject 'Subject' --body-file /tmp/body.txt
```

`--cc`, `--in-reply-to`, `--references` and repeatable `--attach` are supported.
The collector does not ingest drafts. Draft creation is logged in the action audit.
The IMAP special-use Drafts folder takes priority; `purelymail.drafts_folder`
(default `Drafts`) supplies a fallback. No folder is created automatically.
Open your mail client to review and send. This integration has no send operation.
Existing Gmail forwarding and Gmail access are left as configured.

Protocol reference: <https://purelymail.com/docs/setup/technical>.

The mailbox must retain incoming mail. A routing rule that redirects to another
provider can leave no local copy. Inspect routing separately before assuming that
an authenticated but empty inbox proves incoming mail is covered. Do not disable
forwarding or replace routing rules as part of enabling this integration.

## Retaining a local copy while forwarding

`purelymail routing` reads account routing using the separate secret
`purelymail.api_key`. Request this through `herald secret request`; the mailbox
app password and administration API key are different credentials.

The optional `purelymail.keep-copy` red action prepares local delivery for one
mailbox while preserving its existing forwarding destination. Its approval
payload contains `address`, `forward_to` and a complete `expected_rules` snapshot
from `purelymail routing`. Preview it before requesting approval:

```sh
herald act run purelymail.keep-copy --payload-file /tmp/delivery.json --show
herald act run purelymail.keep-copy --payload-file /tmp/delivery.json
```

This action refuses changed routing, an existing exact route or an active Sieve
filter. It uploads and verifies a new filter through TLS ManageSieve, activates
it, then adds an exact self-targeting route for the mailbox. The exact route
ends address rewriting at the local mailbox; the filter redirects to the
existing forwarding destination and explicitly keeps a local copy. The broader
catch-all continues handling other addresses. Existing rules and scripts are
never deleted. The new rule and active filter are read back after the write.
The action changes future delivery; it does not import historical forwarded mail.

ManageSieve writes and route creation refuse calls outside an approved red
performer. If a multi-step change fails, inspect both the active filter and
routing before retrying. The action refuses to overwrite its existing script.
