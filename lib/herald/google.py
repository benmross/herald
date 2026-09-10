"""Access to the user's Google account.

The credentials live in a directory of their own -- `google.credentials_dir` in
the config, `~/.config/google-agent` by default -- holding `credentials.json`
(the OAuth client), `token.json` (the grant), and a small `google_api.py` helper
that both Herald and the `google-workspace` skill import. `herald setup --step
google` creates all of it.

That directory is deliberately not inside Herald. It predates Herald on the
machine this was written on, the skill uses it directly, and a token that
survives reinstalling the program is worth more than a tidy tree.

Never print, log, or commit anything from it.
"""

from __future__ import annotations

import pathlib
import sys

from . import config


def credentials_dir() -> pathlib.Path:
    return pathlib.Path(
        str(config.get("google.credentials_dir", "~/.config/google-agent"))
    ).expanduser()


def _ensure_path() -> None:
    p = str(credentials_dir())
    if p not in sys.path:
        sys.path.insert(0, p)


def available() -> bool:
    return (credentials_dir() / "token.json").exists()


def service(api: str, version: str):
    """An authenticated google-api-python-client resource, e.g. service("gmail", "v1")."""
    _ensure_path()
    from google_api import get_service  # noqa: PLC0415 -- path must be set first
    return get_service(api, version)


def access_token() -> str:
    """Bearer token, for the REST endpoints the Python client does not wrap."""
    _ensure_path()
    from google_api import get_access_token  # noqa: PLC0415
    return get_access_token()
