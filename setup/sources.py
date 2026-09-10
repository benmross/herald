"""Step 6: what else to plug in.

Google is the spine and Telegram is the voice; everything here is optional and
most people will want two or three of them. Each one is a capability with a
credential or a setting behind it, and each is verified before this step calls
itself done -- a source that is switched on but cannot actually be read is worse
than one that is off, because the digest then quietly omits a whole category of
someone's life without saying so.

The bundled extensions appear here too, which is the point of bundling them: an
iMessage reader is useless on a Linux server and close to essential on the Mac a
non-technical person is more likely to be running this on, and neither of those
is a decision the program can make for them.
"""

from __future__ import annotations

import platform
import shutil
import subprocess

from herald import capabilities, config, extensions

from .engine import DONE, PARTIAL, TODO, Field, Outcome, Prompt, State, Step

#: Built-in capabilities the user chooses, with what to ask when they say yes.
OFFERS = [
    {
        "key": "github",
        "label": "What you have been building (GitHub)",
        "help": "Reads your own recent activity through the `gh` CLI you are "
                "already signed into. No token to create.",
        "needs_gh": True,
    },
    {
        "key": "jobs",
        "label": "Job and internship postings",
        "help": "Public, crowdsourced posting lists, scanned against your goals "
                "so a deadline you would have missed reaches you.",
    },
    {
        "key": "calendar_feeds",
        "label": "Calendar feeds (.ics)",
        "help": "Any published calendar: a course's deadlines, a team's "
                "fixtures, a venue's programme. You can add more later.",
        "field": Field(key="feed_url", label="A calendar feed URL",
                       placeholder="https://…/feed.ics",
                       help="Leave blank to set this up later. If it is a "
                            "private link (a Canvas feed is), Herald stores it "
                            "in secrets.json rather than in the config."),
        "field2": Field(key="feed_name", label="What to call it",
                        placeholder="courses",
                        help="A short name; it becomes the source name in the "
                             "ledger."),
    },
]


def _bundled() -> list:
    return [e for e in extensions.all_extensions() if e.bundled]


def _platform_ok(ext) -> bool:
    want = ext.manifest.get("platform")
    return not want or want == platform.system().lower().replace("darwin", "darwin")


def status(state: State) -> tuple[str, str]:
    on = [k for k in capabilities.registry()
          if capabilities.enabled(k) and k not in ("google", "telegram")]
    broken = [k for k in on if not capabilities.available(k)]
    if not state.step("sources").get("chosen"):
        return TODO, "nothing chosen yet"
    if broken:
        return PARTIAL, "needs attention: " + ", ".join(
            f"{k} ({capabilities.missing(k)})" for k in broken)
    return DONE, (", ".join(on) if on else "nothing beyond mail and calendar")


def prompt(state: State) -> Prompt:
    fields = []
    have_gh = bool(shutil.which("gh"))
    for offer in OFFERS:
        if offer.get("needs_gh") and not have_gh:
            continue
        fields.append(Field(key=f"cap_{offer['key']}", label=offer["label"],
                            type="bool", help=offer["help"],
                            default=capabilities.enabled(offer["key"])))
        for extra in ("field", "field2"):
            if offer.get(extra):
                fields.append(offer[extra])

    for ext in _bundled():
        suffix = "" if _platform_ok(ext) else "  — not available on this machine"
        fields.append(Field(
            key=f"ext_{ext.name}", label=f"{ext.name}{suffix}", type="bool",
            default=ext.enabled and _platform_ok(ext),
            help=ext.description + " " + " ".join(
                cap.get("setup_hint", "")
                for cap in ext.manifest.get("provides_capabilities", []))))

    return Prompt(
        title="What else should it read?",
        blurb="All optional. Each one is something your agent can then reason "
              "about — a deadline it can warn you about, a person it can notice "
              "you have not replied to. You can turn any of them on later with "
              "`herald ext` or by running this step again.",
        fields=fields, action="Save these")


def apply(state: State, answers: dict) -> Outcome:
    warnings = []
    for offer in OFFERS:
        key = offer["key"]
        want = bool(answers.get(f"cap_{key}"))
        config.set_user(f"capabilities.{key}", want)
        if not want:
            continue
        if key == "jobs" and not config.get("jobs.feeds"):
            config.set_user("jobs.feeds", {
                "simplify": "https://raw.githubusercontent.com/SimplifyJobs/"
                            "Summer2027-Internships/dev/.github/scripts/listings.json",
            })
        if key == "calendar_feeds":
            url = (answers.get("feed_url") or "").strip()
            name = (answers.get("feed_name") or "feed").strip() or "feed"
            if url:
                feeds = list(config.get("calendar_feeds", []) or [])
                if any(f.get("source") == name for f in feeds):
                    warnings.append(f"a feed called {name!r} already exists; "
                                    f"left it alone")
                else:
                    config.set_secret(f"calendar_feeds.{name}", url)
                    feeds.append({"source": name, "url_secret": f"calendar_feeds.{name}"})
                    config.set_user("calendar_feeds", feeds)
            elif not config.get("calendar_feeds"):
                warnings.append("calendar feeds are on but you have not added "
                                "one yet — `herald config set calendar_feeds` "
                                "or run this step again")

    for ext in _bundled():
        want = bool(answers.get(f"ext_{ext.name}"))
        if want and not _platform_ok(ext):
            warnings.append(f"{ext.name} needs "
                            f"{ext.manifest.get('platform')}, so it stays off")
            want = False
        extensions.set_enabled(ext.name, want)
        for cap in ext.manifest.get("provides_capabilities", []):
            config.set_user(f"capabilities.{cap['key']}", want)
    extensions.sync()

    state.step("sources")["chosen"] = True

    # Verify rather than assume: a capability that is on but unusable is worse
    # than one that is off, because the digest then omits a category of
    # somebody's life without saying so.
    for key in capabilities.registry():
        if capabilities.enabled(key) and not capabilities.available(key):
            cap = capabilities.registry()[key]
            warnings.append(f"{key}: {capabilities.missing(key)}"
                            + (f" — {cap.setup_hint}" if cap.setup_hint else ""))

    on = [k for k in capabilities.registry() if capabilities.available(k)]
    return Outcome(ok=True,
                   message="Reading: " + (", ".join(sorted(on)) or "nothing yet"),
                   warnings=warnings)


STEP = Step(key="sources", title="What else should it read?", optional=True,
            summary="Turn on the sources that apply to you",
            status_fn=status, prompt_fn=prompt, apply_fn=apply)
