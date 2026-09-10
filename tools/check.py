#!/usr/bin/env python
"""Check Herald's architecture still holds.

`herald doctor` asks whether Herald is healthy right now -- credentials, tables,
paths. This asks something different: whether the code still obeys the rules the
design depends on. It is meant to be run by a session that has just edited
Herald, before it restarts anything.

Most of these guard against changes that look harmless and fail silently. An
API key would move every invocation onto metered billing while working
perfectly. `--bare` would skip the subscription login entirely. A timer pointing
at a cycle nobody wrote sits disabled until someone enables it. A collector with
no cadence quietly runs every thirty minutes forever.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import subprocess
import sys
import token
import tokenize

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import (capabilities, config, constitution,  # noqa: E402
                    extensions, migrations, upstream)


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def ok(self, name: str) -> None:
        print(f"  \033[32mPASS\033[0m  {name}")

    def fail(self, name: str, detail: str) -> None:
        self.failures.append(name)
        print(f"  \033[31mFAIL\033[0m  {name}\n        {detail}")

    def warn(self, name: str, detail: str) -> None:
        self.warnings.append(name)
        print(f"  \033[33mWARN\033[0m  {name}\n        {detail}")

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        self.ok(name) if condition else self.fail(name, detail)


def account_pattern(login: str) -> re.Pattern:
    """Where a login name is evidence of a leak rather than an ordinary word.

    A login is very often a real word -- `runner` on a CI machine, `pi` on a
    Raspberry Pi, `admin`, `dev` -- so matching it as prose is useless. Matching
    it after any slash is not much better: a login called `dev` then flags
    /dev/null, /dev/tty and a GitHub branch called dev.

    What actually leaks is a **home** directory or an address: /home/<login>,
    /Users/<login>, ~login, login@host. Anchoring on those is the difference
    between catching a tracked symlink into somebody's home and flagging half
    the shell scripts in the tree.

    Anything outside a home directory is covered by the home-path needles the
    caller builds separately, which match the literal path this install uses.
    """
    account = re.escape(login.lower())
    return re.compile(rf"(?:/home/|/users/|~){account}\b|\b{account}@")


def python_files() -> list[pathlib.Path]:
    """Everything the invariants apply to -- including extensions.

    An extension is someone's own code in their own directory, which is exactly
    why it is checked rather than trusted: the rules it could break (launching
    an engine directly, writing to Google outside gwrite) are the ones whose
    consequences are silent, and a personal collector is no less capable of
    breaking them than a built-in one.
    """
    out = []
    for sub in ("lib", "collectors", "cycles", "tools", "tests"):
        out += sorted((ROOT / sub).rglob("*.py"))
    out += [ROOT / "bin" / "herald", ROOT / "bin" / "herald-telegram"]
    if config.EXTENSIONS.is_dir():
        for ext in sorted(config.EXTENSIONS.iterdir()):
            if (ext / "herald-extension.json").exists():
                out += sorted(ext.rglob("*.py"))
    # This file names every forbidden pattern in order to look for them, so
    # scanning it finds all of them. It flagged itself on the first run.
    me = pathlib.Path(__file__).resolve()
    return [p for p in out
            if p.exists() and "venv" not in p.parts and p.resolve() != me]


def main() -> int:
    c = Checker()
    files = python_files()

    print("\n\033[1msyntax\033[0m")
    bad = []
    for p in files:
        try:
            ast.parse(p.read_text())
        except SyntaxError as e:
            bad.append(f"{p.relative_to(ROOT)}:{e.lineno} {e.msg}")
    c.check(f"{len(files)} python files parse", not bad, "; ".join(bad[:3]))

    print("\n\033[1mthe rule that cannot be broken\033[0m")
    # Only think.py may launch an engine. Everything else must go through it.
    launchers = []
    for p in files:
        if p.name == "think.py":
            continue
        text = p.read_text()
        for m in re.finditer(r'^\s*(?:cmd|proc).*?\[\s*"(claude)"', text, re.M):
            launchers.append(f"{p.relative_to(ROOT)}: launches {m.group(1)!r}")
    c.check("only lib/herald/think.py launches an engine", not launchers,
            "; ".join(launchers[:3]) + ". Route it through think.think() instead.")

    bare = [str(p.relative_to(ROOT)) for p in files if "--bare" in p.read_text()]
    c.check("no --bare anywhere", not bare,
            f"{', '.join(bare)} — bare mode does not read the subscription login, "
            f"so it would demand an API key.")

    keyed = [str(p.relative_to(ROOT)) for p in files
             if re.search(r'os\.environ\[["\']ANTHROPIC_API_KEY', p.read_text())]
    c.check("nothing sets ANTHROPIC_API_KEY", not keyed, ", ".join(keyed))

    # Every write to Google goes through gwrite.py, which logs it to the
    # actions table. A collector or cycle that calls events().insert() itself
    # would work perfectly and leave the digest unable to report it.
    mutators = re.compile(
        r"\.(events|tasks|tasklists|labels|messages|drafts|threads)\(\)\s*"
        r"\.(insert|update|patch|delete|create|modify|send|import_|trash|untrash)\(")
    rogue = []
    for p in files:
        if p.name == "gwrite.py":
            continue
        for m in mutators.finditer(p.read_text()):
            rogue.append(f"{p.relative_to(ROOT)}: {m.group(0)}")
    c.check("only lib/herald/gwrite.py writes to Google", not rogue,
            "; ".join(rogue[:3]) + ". Route it through gwrite so it is logged.")

    # Herald reaches exactly one engine. The Codex fallback came out on
    # 9 September 2026, so a reference to it anywhere is leftover logic
    # rather than a second path that still works.
    # Tokenised rather than grepped, because think.py's own docstring explains
    # why the fallback is gone and a plain grep cannot tell prose about a
    # removal from the removal not having happened. Identifiers and command
    # strings are logic; a paragraph is not.
    stragglers = []
    for path in files:
        try:
            with path.open("rb") as fh:
                for tok in tokenize.tokenize(fh.readline):
                    hit = (tok.type == token.NAME and "codex" in tok.string.lower()) or (
                        tok.type == token.STRING
                        and tok.string.strip("\"'").lower().startswith("codex"))
                    if hit:
                        stragglers.append(f"{path.relative_to(ROOT)}:{tok.start[0]}")
                        break
        except (tokenize.TokenError, SyntaxError, OSError):
            pass
    c.check("no Codex fallback logic remains", not stragglers,
            ", ".join(stragglers) + " -- there is one engine and no fallback.")

    print("\n\033[1mcollectors\033[0m")
    known = set(capabilities.registry())
    collector_files = sorted((ROOT / "collectors").glob("*.py"))
    for ext in sorted(config.EXTENSIONS.glob("*/collectors/*.py")) \
            if config.EXTENSIONS.is_dir() else []:
        collector_files.append(ext)
    for p in collector_files:
        if p.stem.startswith("_"):
            continue
        try:
            label = str(p.relative_to(ROOT))
        except ValueError:
            label = f"{p.parent.parent.name}/{p.parent.name}/{p.name}"
        text = p.read_text()
        missing = [k for k in ("NAME =", "CADENCE_MINUTES =", "REQUIRES =",
                               "def collect(") if k not in text]
        if missing:
            # REQUIRES is the newest of these and the easiest to leave off. A
            # collector without it runs for everyone, including the people who
            # never connected the source it reads -- which is a traceback every
            # thirty minutes about a credential they were never asked for.
            c.fail(label, f"missing {', '.join(missing)}")
            continue
        if "collector.main" not in text:
            c.fail(label, "no collector.main() entry point")
            continue
        m = re.search(r"^REQUIRES\s*=\s*\(([^)]*)\)", text, re.M)
        declared = {part.strip().strip("\"'") for part in (m.group(1) if m else "").split(",")
                    if part.strip()}
        unknown = declared - known
        if unknown:
            c.fail(label, f"requires unknown capabilit{'y' if len(unknown) == 1 else 'ies'} "
                          f"{', '.join(sorted(unknown))} -- add it to "
                          f"capabilities.BUILTIN or to an extension manifest")
        else:
            c.ok(label)

    # A silently missing personal constitution means a session that does not
    # know whose agent it is -- and, on this instance, one that does not know
    # about a severe food allergy. It is generated rather than imported because
    # Claude Code will not follow the ledger symlink to resolve an @import;
    # see lib/herald/constitution.py.
    print("\n\033[1mthe rules a session actually reads\033[0m")
    c.check("config/constitution.md exists", constitution.SHARED.exists(),
            "the shared half of CLAUDE.md is missing")
    if not constitution.PERSONAL.exists():
        c.warn("ledger/identity/constitution.md exists",
               "no personal constitution: a session will not know whose agent "
               "it is. Run `herald setup`.")
    else:
        c.ok("ledger/identity/constitution.md exists")
    # A checkout nobody has set up yet legitimately has neither file. Failing
    # there would mean a fresh clone -- and CI, which is exactly that -- could
    # never be green.
    if not constitution.OUTPUT.exists() and not constitution.PERSONAL.exists():
        c.warn("CLAUDE.md is current", "not generated yet; `herald setup` does it")
    else:
        c.check("CLAUDE.md is current", constitution.current(),
                "it has drifted from config/constitution.md or "
                "ledger/identity/constitution.md. If the difference is an edit "
                "you made to CLAUDE.md itself, move it into one of those two "
                "files -- CLAUDE.md is generated. Then run `herald constitution`.")

    # What this install is allowed to do to its own program, and whether the
    # state on disk still matches that. A tracking install that has drifted
    # cannot fast-forward, and finds out weeks later when an update refuses.
    print("\n\033[1mthis install's relationship to the program\033[0m")
    mode = upstream.mode()
    release = upstream.current_release()
    print(f"  \033[2mmode {mode}, version {upstream.version()}"
          f"{', ' + release if release else ', no release tag'}\033[0m")
    configured = config.CONFIG_PATH.exists()
    if mode == "tracking" and not configured:
        # Nobody has run setup here, so "tracking" is only the default rather
        # than a choice, and demanding the guard that enforces it is wrong.
        # This is a checkout, not an install -- which is exactly what CI is,
        # and how this was found.
        c.warn("this checkout has no install behind it",
               f"no {config.CONFIG_PATH.name} in {config.HOME}; `herald setup` "
               f"decides whether this follows upstream or owns its program")
    elif mode == "tracking":
        local = upstream.local_changes()
        c.check("the program directory is unmodified",
                not local["modified"] and not local["commits"],
                f"{len(local['modified'])} edited file(s) and "
                f"{len(local['commits'])} local commit(s). A tracking install "
                f"updates by fast-forward, which cannot happen over these. "
                f"Reset them, or `herald mode fork` to keep them and stop "
                f"updating.")
        c.check("commits are refused by a git hook", upstream.hook_installed(),
                "the pre-commit hook is missing, so nothing but a prompt is "
                "stopping a session from editing the program. "
                "`herald mode tracking` reinstalls it.")
    elif mode == "maintainer":
        if upstream.hook_installed():
            c.warn("no tracking hook on a maintainer install",
                   "the pre-commit hook that refuses commits is installed here, "
                   "which will block your own work. `herald mode maintainer`.")
        else:
            c.ok("this install may commit and push")
        waiting = upstream.unreleased()
        if waiting:
            c.warn(f"{len(waiting)} commit(s) not in a release",
                   "other installs are still on "
                   f"{release or 'nothing'}; `herald release --preview`")
        else:
            c.ok("every commit is in a release")
    else:
        c.ok("this install is a fork and owns its program")

    print("\n\033[1mmigrations\033[0m")
    pending = migrations.pending()
    if pending:
        c.warn(f"{len(pending)} migration(s) not applied",
               ", ".join(name for name, _ in pending)
               + " -- `herald update` runs them, or migrations.run_pending()")
    else:
        c.ok(f"{len(migrations.all_migrations())} migration(s), all applied")

    print("\n\033[1mextensions\033[0m")
    exts = extensions.all_extensions()
    if not exts:
        print("  \033[2mnone installed\033[0m")
    builtin = {p.stem for p in (ROOT / "collectors").glob("*.py")}
    seen: dict[str, str] = {}
    for ext in exts:
        if "error" in ext.manifest:
            c.fail(f"extensions/{ext.path.name}",
                   f"unreadable manifest: {ext.manifest['error']}")
            continue
        problems = []
        for name in ext.collectors():
            if name in builtin:
                problems.append(f"collector {name} shadows a built-in one")
            if name in seen:
                problems.append(f"collector {name} also comes from {seen[name]}")
            seen[name] = ext.name
        for unit in ext.units():
            if not unit.name.startswith("herald-"):
                problems.append(f"unit {unit.name} should be named herald-*")
        if problems:
            c.fail(f"extensions/{ext.name}", "; ".join(problems))
        else:
            c.ok(f"extensions/{ext.name}"
                 + ("" if ext.enabled else " (disabled)"))

    print("\n\033[1mcycles and their timers\033[0m")
    cycles = {p.stem for p in (ROOT / "cycles").glob("*.py")
              if not p.stem.startswith("_")}
    timers = {}
    for t in sorted((ROOT / "systemd").glob("herald-cycle-*.timer")):
        m = re.search(r"Unit=herald-cycle@(\w+)\.service", t.read_text())
        if m:
            timers[t.name] = m.group(1)
    for name, cycle in timers.items():
        c.check(f"{name} -> cycles/{cycle}.py", cycle in cycles,
                f"no such cycle. Write it or delete the timer.")
    for cycle in sorted(cycles - set(timers.values())):
        c.warn(f"cycles/{cycle}.py has no timer", "runnable by hand only")

    # Two repositories now, and the interesting failures are about which
    # content is in which. The program's repo must never track anything
    # personal; the person's repo holds their whole life and must stay
    # private.
    print("\n\033[1mthe program's repo tracks nothing personal\033[0m")
    # The project's own repository URL is not a leak, and on a machine where the
    # GitHub account and the login share a name it would otherwise trip the
    # account-name needle in every install instruction. Blanked before scanning
    # rather than allowlisted per file, so a home *path* containing the same
    # name is still caught.
    remote_urls = set()
    for line in subprocess.run(["git", "remote", "-v"], cwd=ROOT,
                               capture_output=True, text=True).stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            url = parts[1].removesuffix(".git")
            remote_urls.add(url.lower())
            if "github.com" in url:
                remote_urls.add(url.lower().split("github.com", 1)[1].lstrip(":/"))

    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT,
                             capture_output=True, text=True).stdout.split()
    leaked = [f for f in tracked
              if f.startswith(("ledger/", "config/herald.json", "config/secrets.json"))]
    c.check("no ledger, config or secrets tracked here", not leaked,
            ", ".join(leaked[:5]) + " -- these belong in $HERALD_HOME.")

    ledger = ROOT / "ledger"
    if config.LEGACY_LAYOUT:
        c.warn("ledger is a symlink into $HERALD_HOME",
               "still on the pre-split layout: the ledger is a real directory "
               "inside the program's repo. Run the migration.")
    elif not config.LEDGER.exists() and not ledger.exists():
        c.warn("ledger is a symlink into $HERALD_HOME",
               f"nothing at {config.LEDGER} yet -- run `herald setup`")
    else:
        c.check("ledger is a symlink into $HERALD_HOME",
                ledger.is_symlink() and ledger.resolve() == config.LEDGER.resolve(),
                f"{ledger} does not point at {config.LEDGER}, so every relative "
                f"`ledger/...` path in a prompt or skill is wrong.")

    for path in ("ledger", "config/secrets.json"):
        r = subprocess.run(["git", "check-ignore", path], cwd=ROOT,
                           capture_output=True)
        c.check(f"{path} is gitignored", r.returncode == 0,
                "this would be pushed to GitHub")

    # Until 7 September 2026, ledger/identity/private/ was gitignored and
    # stayed local-only. The user whose instance this was extracted from decided
    # that day that they trusted GitHub's privacy for a repo that is already
    # private, and would rather have real version history for those files than
    # none. That decision's whole premise is that the repo *stays* private, so this checks the fact rather than assuming it: if it ever
    # fails, an address, medical detail and family are one
    # `gh repo edit --visibility public` away from public, and nothing else
    # here would catch it. It follows the ledger: since the split, the repo
    # that matters is $HERALD_HOME's, not the program's.
    print("\n\033[1mprivate ledger content depends on its repo staying private\033[0m")
    home = config.HOME
    if not (home / ".git").exists():
        c.warn("the ledger is versioned", f"{home} is not a git repository, so "
               "there is no history to recover a bad edit from.")
    else:
        remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=home,
                                capture_output=True, text=True).stdout.strip()
        m = re.search(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?$", remote)
        if not remote:
            c.warn("the ledger has an off-box copy",
                   f"{home} has no remote. Local history only -- a dead disk "
                   "takes the identity files and the journal with it.")
        elif not m:
            c.warn("ledger repo privacy is verifiable",
                   f"origin is {remote}, which is not GitHub, so this check "
                   "cannot confirm it is private. Confirm it by hand.")
        else:
            r = subprocess.run(["gh", "repo", "view", f"{m.group(1)}/{m.group(2)}",
                                "--json", "isPrivate"], capture_output=True, text=True)
            c.check("the ledger's github repo is private",
                    r.returncode == 0 and '"isPrivate":true' in r.stdout,
                    f"gh repo view says otherwise ({r.stdout.strip() or r.stderr.strip()})"
                    " -- identity/private/ is versioned there")

    # The program's repo is public. Its own owner is the most likely person to
    # put something personal in it -- Herald was one person's agent for three
    # days before it was anybody else's, and every file here was written while
    # sitting next to a ledger full of real data. So the denylist is derived
    # from *this* install rather than hardcoded: whatever this user's name,
    # address and credentials are, none of them may appear in a tracked file.
    print("\n\033[1mnothing personal in the tracked tree\033[0m")
    # Name parts match on word boundaries. A short surname is a leak inside a
    # full name and not inside an ordinary word that happens to contain it, and
    # a checker that cries wolf on every docstring is a checker somebody
    # switches off.
    needles: dict[str, str] = {}
    name = (config.get("user.name") or "").strip()
    for part in [name, *name.split()]:
        if len(part) >= 3:
            needles[part.lower()] = "the user's name"
    for key in ("user.email", "user.school", "user.city"):
        value = str(config.get(key) or "").strip()
        if len(value) >= 5:
            needles[value.lower()] = key

    # The home paths, which a name-derived denylist misses entirely: five
    # tracked symlinks once pointed at /home/<user>/.herald/extensions/...,
    # publishing a username and the names of somebody's private extensions
    # without containing their name anywhere.
    for path in (config.HOME, pathlib.Path.home()):
        needles[str(path).lower()] = "an absolute path into this user's home"

    # The account name, but only where it is a path or an address. A login is
    # very often an ordinary word -- `runner` on a CI machine, `pi` on a
    # Raspberry Pi, `admin`, `dev` -- and matching it as prose flagged
    # "${{ runner.temp }}", "the test runner's PYTHONPATH" and "the bundled
    # runner" on the public repo's very first CI run. What actually leaks is
    # /home/<login>, ~login, or login@host, so that is what this looks for.
    import getpass
    contextual = [(account_pattern(getpass.getuser()),
                   "this machine's account name, in a path or address")]

    def _walk(node, prefix=""):
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(v, f"{prefix}.{k}" if prefix else k)
        elif isinstance(node, str) and len(node) >= 12 and not node.startswith("_"):
            needles[node.lower()] = f"a value from secrets.json ({prefix})"

    _walk(config.secrets())

    #: shapes that are a credential wherever they appear, whoever's they are
    SHAPES = [
        (re.compile(r"\b\d{8,}:[A-Za-z0-9_-]{30,}\b"), "a Telegram bot token"),
        (re.compile(r"\b\d+-[a-z0-9]{20,}\.apps\.googleusercontent\.com\b"),
         "a Google OAuth client id"),
        (re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}"), "a Google access token"),
        (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
        (re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"), "a GitHub token"),
        (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "an API key"),
    ]

    # The project's own repository URL is not a leak, and on a machine where the
    # GitHub account and the login share a name it would otherwise trip the
    # account-name needle in every install instruction. Blanked before scanning
    # rather than allowlisted per file, so a home *path* containing the same
    # name is still caught.
    remote_urls = set()
    for line in subprocess.run(["git", "remote", "-v"], cwd=ROOT,
                               capture_output=True, text=True).stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            url = parts[1].removesuffix(".git")
            remote_urls.add(url.lower())
            if "github.com" in url:
                remote_urls.add(url.lower().split("github.com", 1)[1].lstrip(":/"))

    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT,
                             capture_output=True, text=True).stdout.split()
    # A copyright line is attribution, which is the one place the author's name
    # belongs in a public repository. Nothing else is exempt.
    ALLOWED = {"LICENSE"}
    hits = []
    for rel in tracked:
        if rel in ALLOWED:
            continue
        path = ROOT / rel
        if path.is_symlink():
            # A symlink's *target* is content too. Five of them were tracked
            # here pointing at $HERALD_HOME/extensions/..., which would have
            # published the names of private extensions and a username, and the
            # scan skipped them because they are not files.
            text = str(pathlib.Path.readlink(path))
        elif not path.exists():
            continue
        else:
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
        lowered = text.lower()
        for url in remote_urls:
            if url:
                lowered = lowered.replace(url, "<this project's repo>")
        for needle, why in needles.items():
            pattern = re.compile(rf"\b{re.escape(needle)}\b")
            if not pattern.search(lowered):
                continue
            line = next((i + 1 for i, l in enumerate(text.splitlines())
                         if pattern.search(l.lower())), 0)
            hits.append(f"{rel}:{line} contains {why}")
        for pattern, why in contextual:
            m = pattern.search(lowered)
            if m:
                line = next((i + 1 for i, l in enumerate(text.splitlines())
                             if pattern.search(l.lower())), 0)
                hits.append(f"{rel}:{line} contains {why}")
        for pattern, why in SHAPES:
            m = pattern.search(text)
            if m:
                hits.append(f"{rel}: looks like {why}")
    c.check(f"{len(tracked)} tracked files carry nothing personal", not hits,
            "; ".join(sorted(set(hits))[:6])
            + ". This repository is public: move it to $HERALD_HOME.")

    print("\n\033[1mexecutables\033[0m")
    import os
    for b in ("bin/herald", "bin/herald-brain", "bin/herald-telegram",
              "tools/install-units.sh"):
        p = ROOT / b
        c.check(b, p.exists() and os.access(p, os.X_OK), "not executable")

    print("\n\033[1minstalled units match the repo\033[0m")
    dest = pathlib.Path.home() / ".config" / "systemd" / "user"
    # Units are templates: {{ROOT}} and {{GOOGLE}} are filled in at install
    # time, so the installed copy is compared against the rendered source
    # rather than the raw one.
    sys.path.insert(0, str(ROOT))
    from setup import services  # noqa: PLC0415
    for p in services.unit_sources():
        live = dest / p.name
        rendered = services.render(p.read_text())
        if not live.exists():
            c.warn(p.name, "not installed — run `herald services install`")
        elif live.read_text() != rendered:
            c.warn(p.name, "installed copy differs — run `herald services install`")
        else:
            c.ok(p.name)
    ext_units = {u.name for ext in extensions.all_extensions() for u in ext.units()}
    for live in sorted(dest.glob("herald-*")):
        if live.name in ext_units or (ROOT / "systemd" / live.name).exists():
            continue
        c.warn(live.name, "installed but belongs to no repo or extension — orphan")

    print()
    if c.failures:
        print(f"\033[31m{len(c.failures)} failure(s)\033[0m"
              + (f", {len(c.warnings)} warning(s)" if c.warnings else ""))
        return 1
    print(f"\033[32marchitecture holds\033[0m"
          + (f" ({len(c.warnings)} warning(s))" if c.warnings else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
