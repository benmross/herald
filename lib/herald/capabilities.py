"""What this install can actually do, and what each part of it needs.

Herald shipped with thirteen collectors and ran all of them, because it had
exactly one user and everything the user owned was connected. That does not survive
contact with a second person: someone with no Canvas account, no location
server and no job hunt should not see five collectors failing every half hour
at things they never asked for.

So a collector declares what it needs:

    NAME = "gcal"
    CADENCE_MINUTES = 30
    REQUIRES = ("google",)

and a capability is available when the user turned it on *and* the credentials
it names are actually present. Unavailable is not failure: the collector is
skipped with a reason, reported as off rather than broken, and never counts
towards a failure streak. The distinction matters because "you never set this
up" and "this is broken" want completely different responses from the reader.

Extensions add their own capabilities through their manifest, so a personal
collector can gate on a personal source without that source's name appearing
anywhere in the program.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field

from . import config


@dataclass(frozen=True)
class Capability:
    key: str
    title: str
    summary: str
    #: dotted paths into secrets.json that must exist and be non-empty
    secrets: tuple[str, ...] = ()
    #: dotted paths into the merged config that must exist and be non-empty
    settings: tuple[str, ...] = ()
    #: files that must exist; "~" is expanded, and a config path in braces is
    #: substituted, e.g. "{google.credentials_dir}/token.json"
    files: tuple[str, ...] = ()
    #: the extension that provides it, or None for the built-ins
    extension: str | None = None
    setup_hint: str = ""


BUILTIN: tuple[Capability, ...] = (
    Capability(
        key="google",
        title="Google account",
        summary="Mail, calendars, tasks and contacts. The spine of everything "
                "else: without it Herald knows nothing about your days.",
        files=("{google.credentials_dir}/token.json",),
        setup_hint="herald setup --step google",
    ),
    Capability(
        key="telegram",
        title="Telegram",
        summary="How Herald reaches you and how you talk back, from anywhere.",
        secrets=("telegram.bot_token",),
        setup_hint="herald setup --step telegram",
    ),
    Capability(
        key="github",
        title="GitHub activity",
        summary="What you have actually been building lately, through the `gh` "
                "CLI you are already signed into.",
    ),
    Capability(
        key="jobs",
        title="Job and internship feeds",
        summary="Public posting lists, scanned for things worth applying to.",
        settings=("jobs.feeds",),
    ),
    Capability(
        key="calendar_feeds",
        title="Calendar feeds",
        summary="Any .ics URL — a course calendar, a team schedule, a public "
                "events feed — read into the ledger as facts.",
        settings=("calendar_feeds",),
    ),
)


def _extension_capabilities() -> list[Capability]:
    """Capabilities declared by enabled extensions.

    Read from the manifests rather than imported, because asking an extension
    what it provides must never mean running its code.
    """
    out: list[Capability] = []
    manifests = []
    for root in (config.ROOT / "extensions", config.EXTENSIONS):
        if root.is_dir():
            manifests += sorted(root.glob("*/herald-extension.json"))
    for manifest in manifests:
        try:
            data = json.loads(manifest.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        name = data.get("name") or manifest.parent.name
        bundled = manifest.parent.parent == config.ROOT / "extensions"
        override = config.get(f"extensions.{name}.enabled")
        enabled = bool(override) if override is not None \
            else bool(data.get("enabled", not bundled))
        if not enabled:
            continue
        for cap in data.get("provides_capabilities", []) or []:
            if isinstance(cap, str):
                cap = {"key": cap}
            out.append(Capability(
                key=cap["key"],
                title=cap.get("title", cap["key"]),
                summary=cap.get("summary", f"provided by the {name} extension"),
                secrets=tuple(cap.get("secrets", ())),
                settings=tuple(cap.get("settings", ())),
                files=tuple(cap.get("files", ())),
                extension=name,
                setup_hint=cap.get("setup_hint", ""),
            ))
    return out


def registry() -> dict[str, Capability]:
    """Every capability this install knows about, built-in and extension."""
    out = {c.key: c for c in BUILTIN}
    for cap in _extension_capabilities():
        out[cap.key] = cap
    return out


def enabled(key: str) -> bool:
    """Whether the user turned this on. Unknown keys are off."""
    value = config.get(f"capabilities.{key}")
    if isinstance(value, dict):
        return bool(value.get("enabled", True))
    return bool(value)


def _resolve_file(pattern: str) -> pathlib.Path:
    out = pattern
    while "{" in out and "}" in out:
        start, end = out.index("{"), out.index("}")
        out = out[:start] + str(config.get(out[start + 1:end], "")) + out[end + 1:]
    return pathlib.Path(out).expanduser()


def missing(key: str) -> str | None:
    """Why this capability is not usable, or None if it is.

    Order matters: "you have not turned this on" comes before "the credential
    it would need is absent", because the second is not a problem for someone
    who never wanted the first.
    """
    cap = registry().get(key)
    if cap is None:
        return f"unknown capability {key!r}"
    if not enabled(key):
        return "not enabled"
    for path in cap.secrets:
        if not config.secret(path):
            return f"secrets.json has no {path}"
    for path in cap.settings:
        if not config.get(path):
            return f"config has no {path}"
    for pattern in cap.files:
        resolved = _resolve_file(pattern)
        if not resolved.exists():
            return f"{resolved} is missing"
    return None


def available(key: str) -> bool:
    return missing(key) is None


def unmet(requires) -> list[tuple[str, str]]:
    """[(capability, why)] for everything in `requires` that is not usable."""
    out = []
    for key in requires or ():
        why = missing(key)
        if why:
            out.append((key, why))
    return out
