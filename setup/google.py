"""Step 4: the Google account, and the console walkthrough that guards it.

This is the longest step and the only one where the user has to go and use
somebody else's website. Google does not offer a way to hand an application
access to your own account without creating a project in the Cloud Console, so
the honest thing is to walk through it a screen at a time and say what each
scary-looking page actually means -- particularly the "Google hasn't verified
this app" warning, which is correct, alarming, and unavoidable: the app is
theirs, they made it four minutes ago, and nobody has verified it because
nobody else will ever use it.

What this produces:

    ~/.config/google-agent/          (or google.credentials_dir)
      credentials.json    the OAuth client they downloaded          0600
      token.json          the grant, refreshed automatically        0600
      google_api.py       the helper Herald and the skill import
      authorize.py        re-running the consent flow by hand

The token is what everything else in Herald reads mail and calendars with, and
this directory is deliberately outside the Herald checkout so that reinstalling
the program does not mean doing this again.
"""

from __future__ import annotations

import json
import pathlib
import re
import threading
import urllib.parse
import wsgiref.simple_server  # noqa: F401  (documents that we could, but do not)
from http.server import BaseHTTPRequestHandler, HTTPServer

from herald import config, google as google_mod

from .engine import BLOCKED, DONE, TODO, Field, Outcome, Prompt, State, Step

SCOPES = [
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/tasks",
]

APIS = [
    ("Gmail API", "gmail.googleapis.com"),
    ("Google Calendar API", "calendar-json.googleapis.com"),
    ("Google Drive API", "drive.googleapis.com"),
    ("Google Docs API", "docs.googleapis.com"),
    ("Google Sheets API", "sheets.googleapis.com"),
    ("Google Tasks API", "tasks.googleapis.com"),
    ("People API", "people.googleapis.com"),
]

DEFAULT_PORT = 8765

GOOGLE_API_PY = '''"""Shared authenticated Google API access for local agents.

Written by Herald's setup. Imported by Herald itself and by the
`google-workspace` skill; both find it by putting this directory on sys.path.
"""

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

CONFIG_DIR = Path(__file__).resolve().parent
TOKEN_FILE = CONFIG_DIR / "token.json"


def get_credentials():
    """Load, refresh, and persist the shared OAuth credentials."""
    credentials = Credentials.from_authorized_user_file(str(TOKEN_FILE))
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        TOKEN_FILE.write_text(credentials.to_json())
        TOKEN_FILE.chmod(0o600)
    if not credentials.valid:
        raise RuntimeError(
            "Google OAuth token is invalid; reauthorize with "
            "`herald setup --step google`."
        )
    return credentials


def get_service(api, version):
    """Return an authenticated google-api-python-client service."""
    return build(api, version, credentials=get_credentials(), cache_discovery=False)


def get_access_token():
    """Return a valid OAuth bearer token for direct REST requests."""
    return get_credentials().token
'''

AUTHORIZE_PY = '''#!/usr/bin/env python
"""Re-run the Google consent flow by hand.

`herald setup --step google` does this for you. This exists for the case where
the token is unrecoverable and you would rather not run the wizard.
"""

import os
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

CONFIG_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"
TOKEN_FILE = CONFIG_DIR / "token.json"
SCOPES = {scopes}


def main():
    port = int(os.environ.get("GOOGLE_AGENT_OAUTH_PORT", "{port}"))
    print(
        "If this machine has no browser, forward the port from the computer "
        "you are sitting at:\\n"
        f"    ssh -N -L 127.0.0.1:{{port}}:127.0.0.1:{{port}} USER@THIS-MACHINE\\n"
        "then open the URL below in that computer's browser.\\n"
    )
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(host="127.0.0.1", port=port, open_browser=False,
                                  authorization_prompt_message="Open this URL:\\n{{url}}")
    TOKEN_FILE.write_text(creds.to_json())
    TOKEN_FILE.chmod(0o600)
    print(f"\\nAuthorized. Token written to {{TOKEN_FILE}}")


if __name__ == "__main__":
    sys.exit(main())
'''

CONSOLE_STEPS = """\
Google will not let a program read your account until you have told Google
about that program yourself. That takes about five minutes on Google's website,
and you only do it once. Each numbered item below is one page to visit; the
links open in a new tab.

**1. Make a project.** Open <https://console.cloud.google.com/projectcreate>.
Call it anything you like (`herald` is fine) and click Create. Wait for it to
finish, and make sure it is the project selected in the bar at the top of the
page.

**2. Switch on the seven services Herald reads.** Each link opens one of them
in your project. Click **Enable** on each, then come back here:

{api_links}

**3. Set up the consent screen.** Open
<https://console.cloud.google.com/auth/overview>. If it asks, choose
**External**. That is the only choice on a personal Google account, and it does
not make anything public. Give the app a name (`Herald`), enter your own email
address where it asks for support and developer contact, and save.

**4. Add yourself as a test user.** Still on the consent screen, find
**Audience**, then **Test users**, then **Add users**, and add your own Google
address. This is the step people miss. Without it, the sign-in at the end
fails with the words "access_denied".

**5. Create the credentials.** Open
<https://console.cloud.google.com/auth/clients>, click **Create client**, choose
**Desktop app** as the type, give it any name, and click Create. Then click the
**download** icon next to the client you just made. You get a small file ending
in `.json`.

Open that file in any text editor, copy everything in it, and paste it into the
box below. It is not a password. It identifies the app you just made, not your
account, and it stays on this computer.
"""

VERIFY_WARNING = """\
When you sign in, Google will say **"Google hasn't verified this app"**. That is
expected. The app is the one you created a few minutes ago, nobody has reviewed
it, and nobody else will ever use it. Click **Advanced**, then **Go to Herald
(unsafe)**, to carry on. You are giving your own program access to your own
account.
"""


def credentials_dir() -> pathlib.Path:
    return google_mod.credentials_dir()


def _ssh_note() -> str:
    """The port-forward, only when there is an SSH session to forward through."""
    from .engine import over_ssh  # noqa: PLC0415
    if not over_ssh():
        return ""
    return (" You are connected over SSH, so the end of the sign-in has to reach "
            "this machine. If you started the browser setup with the `ssh -N -L` "
            "line it printed, that is already covered. Otherwise run\n\n"
            f"    ssh -N -L 127.0.0.1:{DEFAULT_PORT}:127.0.0.1:{DEFAULT_PORT} "
            "USER@THIS-MACHINE\n\non the computer you are sitting at first.")


# --------------------------------------------------------------------------
# The consent flow, done by hand rather than with run_local_server, because
# both frontends need the URL *before* anything blocks: the terminal prints it,
# and the browser wizard has to show it on a page while the flow waits.
# --------------------------------------------------------------------------

class _Catcher(BaseHTTPRequestHandler):
    code: str | None = None
    error: str | None = None

    def do_GET(self):                                               # noqa: N802
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Catcher.code = (params.get("code") or [None])[0]
        _Catcher.error = (params.get("error") or [None])[0]
        body = ("<h2>You can close this tab.</h2><p>Herald has what it needs.</p>"
                if _Catcher.code else
                f"<h2>That did not work.</h2><p>{_Catcher.error or 'no code returned'}</p>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):                                   # noqa: A002
        pass


def authorize(port: int = DEFAULT_PORT, on_url=None, timeout: int = 900) -> str:
    """Run the consent flow. Returns the authorized address.

    `on_url` is called with the sign-in URL as soon as it exists, which is what
    lets a browser wizard show it on a page and a terminal print it while this
    blocks waiting for the redirect.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: PLC0415

    directory = credentials_dir()
    client = directory / "credentials.json"
    if not client.exists():
        raise FileNotFoundError(f"no credentials.json in {directory}")

    flow = InstalledAppFlow.from_client_secrets_file(str(client), SCOPES)
    flow.redirect_uri = f"http://localhost:{port}/"
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent",
                                         include_granted_scopes="true")
    _Catcher.code = _Catcher.error = None
    server = HTTPServer(("127.0.0.1", port), _Catcher)
    server.timeout = timeout
    if on_url:
        on_url(auth_url)

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout)
    server.server_close()

    if _Catcher.error:
        raise RuntimeError(f"Google answered \"{_Catcher.error}\". The usual "
                           f"cause is step 4: you have not been added as a test "
                           f"user on the consent screen.")
    if not _Catcher.code:
        raise TimeoutError("nothing came back from Google. If you are setting "
                           "this up on another computer over SSH, the "
                           "port-forward described above has to be running "
                           "before you open the link.")

    flow.fetch_token(code=_Catcher.code)
    token = directory / "token.json"
    token.write_text(flow.credentials.to_json())
    token.chmod(0o600)
    return whoami()


_whoami_cache: dict = {}


def whoami() -> str:
    """The address the stored token actually belongs to.

    Cached against the token file's modification time, so the wizard's page
    loads do not each cost a round trip to Google; a new sign-in writes the
    file and invalidates it.
    """
    token = credentials_dir() / "token.json"
    try:
        stamp = token.stat().st_mtime
    except OSError:
        stamp = None
    if _whoami_cache.get("stamp") == stamp and "email" in _whoami_cache:
        return _whoami_cache["email"]
    profile = google_mod.service("gmail", "v1").users().getProfile(userId="me").execute()
    email = profile.get("emailAddress", "")
    _whoami_cache.update(stamp=stamp, email=email)
    return email


def install_helpers() -> pathlib.Path:
    """Put the helper and the manual re-auth script beside the credentials.

    Existing files are left alone. An install that predates Herald may have a
    helper of its own with scopes this wizard does not know about, and
    overwriting it would quietly narrow what a later re-authorisation asks for.
    """
    directory = credentials_dir()
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    if not (directory / "google_api.py").exists():
        (directory / "google_api.py").write_text(GOOGLE_API_PY)
    if not (directory / "authorize.py").exists():
        (directory / "authorize.py").write_text(
            AUTHORIZE_PY.replace("{scopes}", json.dumps(SCOPES, indent=4))
                        .replace("{port}", str(DEFAULT_PORT)))
    return directory


# --------------------------------------------------------------------------

def _stage(state: State) -> str:
    directory = credentials_dir()
    if not (directory / "credentials.json").exists():
        return "console"
    if not (directory / "token.json").exists():
        return "authorize"
    return "done"


def status(state: State) -> tuple[str, str]:
    if not google_mod.available():
        return TODO, "not connected"
    try:
        return DONE, f"connected as {whoami()}"
    except Exception as exc:                                        # noqa: BLE001
        return BLOCKED, f"the saved sign-in no longer works ({type(exc).__name__}: {exc})"


def prompt(state: State) -> Prompt:
    stage = _stage(state)
    if stage == "console":
        links = "\n".join(
            f"- [{name}](https://console.cloud.google.com/apis/library/{host})"
            for name, host in APIS)
        return Prompt(
            title="Connect your Google account",
            blurb=CONSOLE_STEPS.format(api_links=links),
            fields=[Field(
                key="client_json", label="What is in the downloaded file",
                type="textarea", rows=8, required=True,
                placeholder='{"installed":{"client_id":"...","project_id":"..."}}',
                help="Paste the whole file, from the first { to the last }. It "
                     "is kept on this computer, readable only by you, and is "
                     "only ever sent to Google.")],
            action="Save and continue")
    if stage == "authorize":
        return Prompt(
            title="Sign in to Google",
            blurb="Now the part where you give permission.\n\n" + VERIFY_WARNING
                  + "\n\nWhen you press the button, a sign-in link appears. Open "
                    "it, choose the account this Herald is for, and tick every "
                    "permission it asks for. Herald asks for mail, calendar, "
                    "contacts, tasks, Drive, Docs and Sheets because those are "
                    "the things it reads and writes for you." + _ssh_note(),
            fields=[], immediate=True, action="Get my sign-in link")
    return Prompt(
        title="Google is connected",
        blurb=f"Signed in as **{status(state)[1].removeprefix('connected as ')}**. "
              f"Herald can read your mail, calendars, tasks and contacts, and "
              f"add to the calendars you tell it to.\n\nRunning this step again "
              f"signs in afresh, which is what to do if it ever stops working.",
        fields=[], immediate=True, action="Sign in again")


def apply(state: State, answers: dict) -> Outcome:
    stage = _stage(state)
    directory = install_helpers()

    if stage == "console":
        raw = (answers.get("client_json") or "").strip()
        if not raw:
            return Outcome(ok=False, message="Nothing pasted.")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return Outcome(ok=False, message="That does not look like the whole file.",
                           detail="Paste everything in it, starting with { and "
                                  "ending with }. (The exact problem: "
                                  f"{exc}.)")
        if not (parsed.get("installed") or parsed.get("web")):
            return Outcome(
                ok=False,
                message="That file is not the kind Herald needs.",
                detail="If it contains the words \"service_account\", the "
                       "wrong kind of credential was created. Go back to step "
                       "5 and choose Desktop app.")
        if parsed.get("web"):
            return Outcome(
                ok=False, message="That file is for a web application.",
                detail="Go back to step 5, create a new client, and choose "
                       "**Desktop app** as the type.")
        path = directory / "credentials.json"
        path.write_text(json.dumps(parsed, indent=2))
        path.chmod(0o600)
        config.set_user("google.credentials_dir", str(directory))
        return Outcome(ok=True, message="Saved. Now sign in.", more=True)

    # authorize, or re-authorize
    port = int(answers.get("port") or DEFAULT_PORT)
    on_url = answers.get("_on_url")
    email = authorize(port=port, on_url=on_url)
    config.set_user("capabilities.google", True)
    if not config.get("user.email"):
        config.set_user("user.email", email)
    state.step("google")["email"] = email
    return Outcome(ok=True, message=f"Connected as {email}.")


STEP = Step(key="google", title="Connect your Google account",
            summary="Mail, calendar, tasks and contacts: the core of everything",
            status_fn=status, prompt_fn=prompt, apply_fn=apply)
