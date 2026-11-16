"""Run Cloudflare's `cf` CLI with the install's stored API token.

`cf auth login` and `wrangler login` are browser OAuth flows whose tokens
expire and cannot be renewed from a non-interactive session, which left
deploys waiting on the person to sign in again over SSH. `cf` (and Wrangler)
prefer `CLOUDFLARE_API_TOKEN` over any OAuth profile, so `herald cf ...` puts
the token from secrets.json into the child's environment and nowhere else: it
is never printed, logged, or passed on a command line.

Settings (config.json, all optional):

    cloudflare.token_secret   dotted secrets.json path (default cloudflare.api_token)
    cloudflare.account_id     account to act on when the token can see several
    cloudflare.cli            executable to run (default: `cf` on PATH)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from herald import config

DEFAULT_SECRET = "cloudflare.api_token"
INSTALL_HINT = "install Cloudflare's CLI with `npm install --global cf` (Node.js 22.18 or later)"
# A global API key or OAuth profile would silently take a different identity
# than the scoped token this install was given.
SCRUBBED = ("CLOUDFLARE_API_KEY", "CLOUDFLARE_EMAIL", "CLOUDFLARE_AUTH_PROFILE")


class Unavailable(RuntimeError):
    pass


def settings() -> dict:
    value = config.load().get("cloudflare", {})
    return value if isinstance(value, dict) else {}


def executable() -> str:
    configured = settings().get("cli") or "cf"
    found = shutil.which(configured)
    if not found:
        raise Unavailable(f"`{configured}` was not found; {INSTALL_HINT}")
    return found


def environment(base: dict | None = None) -> dict:
    """The environment a Cloudflare tool should run with."""
    path = settings().get("token_secret") or DEFAULT_SECRET
    token = config.secret(path)
    if not isinstance(token, str) or not token.strip():
        raise Unavailable(
            f"secrets.json has no {path}; store an API token with "
            f"`herald secret request {path} --why 'Cloudflare access'`"
        )
    env = {key: value for key, value in (base if base is not None else os.environ).items()
           if key not in SCRUBBED}
    env["CLOUDFLARE_API_TOKEN"] = token.strip()
    account = settings().get("account_id")
    if isinstance(account, str) and account.strip():
        env["CLOUDFLARE_ACCOUNT_ID"] = account.strip()
    return env


def run(arguments: list[str], cwd: str | None = None) -> int:
    """Run `cf` with the token, inheriting stdio so output streams as usual."""
    try:
        command = [executable(), *arguments]
        env = environment()
    except Unavailable as error:
        print(f"herald cf: {error}", file=sys.stderr)
        return 2
    try:
        return subprocess.run(command, env=env, cwd=cwd).returncode
    except KeyboardInterrupt:
        return 130


def main(arguments: list[str]) -> int:
    if arguments[:1] in (["-h"], ["--help"], []):
        print(
            "usage: herald cf <cf arguments...>\n\n"
            "Runs Cloudflare's `cf` CLI with the API token stored at "
            f"secrets.json {settings().get('token_secret') or DEFAULT_SECRET}.\n"
            "Examples: herald cf auth whoami | herald cf deploy | herald cf d1 list\n"
            "`cf --help` and `cf cli search <words>` list every command."
        )
        return 0
    return run(arguments)
