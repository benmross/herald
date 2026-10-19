"""Extensions: the code that is about one person's life rather than about Herald.

Herald shipped with a collector that scraped nine of one university's event
sites, another that read a Canvas session out of Safari on a particular Mac,
and a third that read a message archive only its author runs. All three are
good code and none of them belong in a program other people install: an
architecture that knows one university's event-feed ids has quietly decided
its users are students there.

So they moved out, into the same private directory as the ledger:

    $HERALD_HOME/extensions/<name>/
      herald-extension.json    the manifest -- what it is, what it provides,
                               what it needs, whether it is on
      collectors/*.py          discovered by `herald collect`, same contract
      cycles/*.py              runnable by `herald cycle`
      lib/                     importable by its own code
      bin/                     its commands; each gets a launcher in ~/.local/bin
      systemd/                 units, installed with the built-in ones
      skills/                  symlinked where sessions will load them
      hooks/hooks.json         hooks, merged into .claude/settings.json and .codex/hooks.json
      red.py                   red actions of its own, carried out only by lib/herald/red.py
      tests/                   run with the rest of the suite

The manifest is **read, never imported**. Asking an extension what it provides
must not mean running its code, or `herald status` on a machine with a broken
extension would be a traceback instead of an answer.

Nothing here is a plugin system in the ambitious sense: there is no API to
program against beyond the one collectors and cycles already use, no hook
points inside Herald, no versioned contract. An extension is just Herald's own
directory layout, somewhere private, discovered rather than registered. That is
enough to make one shareable -- it is a directory, so it is a git repo -- and
small enough that nothing has to be maintained to keep it working.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass

from . import config

MANIFEST = "herald-extension.json"

#: Extensions ship in two places. `$HERALD_ROOT/extensions` holds the ones
#: Herald distributes -- optional, off until someone turns them on, and
#: useless to most people (reading a Mac's iMessage database is not something
#: a Linux server can do). `$HERALD_HOME/extensions` holds the user's own.
#: A user extension shadows a bundled one of the same name, because a person
#: modifying what they were shipped should get what they modified.
BUNDLED_ROOT = config.ROOT / "extensions"

#: Where an enabled extension's skills are linked so sessions find them.
SKILLS_DIR = config.ROOT / ".claude" / "skills"
#: The same links again, where the Codex CLI looks for a repository's skills.
CODEX_SKILLS_DIR = config.ROOT / ".agents" / "skills"
#: Claude Code project settings, generated: base + every enabled extension's hooks.
SETTINGS_PATH = config.ROOT / ".claude" / "settings.json"
#: The Codex copy of the same hooks, generated from the same sources.
CODEX_HOOKS_PATH = config.ROOT / ".codex" / "hooks.json"
SETTINGS_BASE = config.ROOT / "config" / "claude-settings.base.json"


@dataclass(frozen=True)
class Extension:
    name: str
    path: pathlib.Path
    manifest: dict

    @property
    def bundled(self) -> bool:
        return self.path.parent == BUNDLED_ROOT

    @property
    def enabled(self) -> bool:
        """On or off for this install.

        The user's config decides, not the manifest, because a bundled
        extension's manifest is Herald's file: writing enablement into it
        would mean a `git pull` could turn somebody's iMessage reader on or
        off. The manifest only supplies the default, and a bundled extension
        defaults to off -- shipped is not the same as wanted.
        """
        override = config.get(f"extensions.{self.name}.enabled")
        if override is not None:
            return bool(override)
        return bool(self.manifest.get("enabled", not self.bundled))

    @property
    def description(self) -> str:
        return self.manifest.get("description", "")

    def dir(self, sub: str) -> pathlib.Path | None:
        d = self.path / sub
        return d if d.is_dir() else None

    def collectors(self) -> dict[str, pathlib.Path]:
        d = self.dir("collectors")
        return {p.stem: p for p in sorted(d.glob("*.py"))
                if not p.stem.startswith("_")} if d else {}

    def cycles(self) -> dict[str, pathlib.Path]:
        d = self.dir("cycles")
        return {p.stem: p for p in sorted(d.glob("*.py"))
                if not p.stem.startswith("_")} if d else {}

    def units(self) -> list[pathlib.Path]:
        d = self.dir("systemd")
        if not d:
            return []
        return sorted(p for p in d.iterdir()
                      if p.suffix in (".service", ".timer"))

    def skills(self) -> list[pathlib.Path]:
        d = self.dir("skills")
        return sorted(p for p in d.iterdir() if p.is_dir()) if d else []

    def hooks(self) -> dict:
        """This extension's Claude Code hooks, with paths made absolute.

        `{extension}` in any command is replaced with this extension's own
        directory, so a hook can name a script it ships without knowing where
        the user keeps their Herald home.
        """
        path = self.path / "hooks" / "hooks.json"
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return json.loads(json.dumps(raw).replace("{extension}", str(self.path)))


    def own_repo(self) -> bool:
        """Whether this directory is a git repository of its own, rather than
        a folder inside the ledger's repository."""
        return (self.path / ".git").exists()

    def source(self) -> str | None:
        """Where it was installed from: its own repo's origin, if it has one."""
        if not self.own_repo():
            return None
        r = _git(self.path, "remote", "get-url", "origin")
        return (r.stdout.strip() or None) if r.returncode == 0 else None

    def setup_items(self) -> list[dict]:
        """What this extension asks of the person installing it.

        The manifest's `setup` list describes each value: `key`, `prompt`,
        `example`, `default`, `type` (string, list, object, number, boolean),
        `secret` and `required`. Anything a capability names under `settings`
        or `secrets` without a description still appears, with a bare prompt,
        so an older manifest is asked about too. `set` says whether this
        install already has it; a secret's value is never read out.
        """
        items = [dict(i) for i in self.manifest.get("setup", []) or []
                 if isinstance(i, dict) and i.get("key")]
        described = {i["key"] for i in items}
        for cap in self.manifest.get("provides_capabilities", []) or []:
            if not isinstance(cap, dict):
                continue
            for key in cap.get("settings", ()):
                if key not in described:
                    items.append({"key": key})
                    described.add(key)
            for key in cap.get("secrets", ()):
                if key not in described:
                    items.append({"key": key, "secret": True})
                    described.add(key)
        for item in items:
            item.setdefault("required", True)
            item.setdefault("prompt", item["key"])
            if item.get("secret"):
                item["set"] = bool(config.secret(item["key"]))
            else:
                item["set"] = config.get(item["key"]) not in (None, "", [], {})
        return items

    def settings_needed(self) -> list[tuple[str, str]]:
        """[(path, "setting" | "secret")] that are required and unset."""
        return [(i["key"], "secret" if i.get("secret") else "setting")
                for i in self.setup_items() if i["required"] and not i["set"]]


def _expand(value):
    """`{HOME}` and `{ROOT}` in a default, so a manifest can say where things
    go on whoever's machine it lands on."""
    if isinstance(value, str):
        return value.replace("{HOME}", str(config.HOME)).replace("{ROOT}", str(config.ROOT))
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def apply_defaults(ext: "Extension") -> list[str]:
    """Write each unset setting that has a default into the user's config.

    Only into the user's config.json, never the extension's files, and never
    over a value they set. Run at install and again after an update, so a
    setting a new version adds arrives with its default.
    """
    written = []
    for item in ext.setup_items():
        if item.get("secret") or item["set"] or "default" not in item:
            continue
        config.set_user(item["key"], _expand(item["default"]))
        written.append(item["key"])
    return written


def _git(cwd: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _load(path: pathlib.Path) -> Extension | None:
    manifest_path = path / MANIFEST
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        manifest = {"name": path.name, "error": str(exc), "enabled": False}
    return Extension(name=manifest.get("name") or path.name, path=path,
                     manifest=manifest)


def all_extensions() -> list[Extension]:
    """Bundled first, then the user's, which shadow them by name."""
    found: dict[str, Extension] = {}
    for root in (BUNDLED_ROOT, config.EXTENSIONS):
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            if not path.is_dir():
                continue
            ext = _load(path)
            if ext:
                found[ext.name] = ext
    return [found[name] for name in sorted(found)]


def enabled() -> list[Extension]:
    return [e for e in all_extensions() if e.enabled]


def find(name: str) -> Extension | None:
    for ext in all_extensions():
        if ext.name == name or ext.path.name == name:
            return ext
    return None


def set_enabled(name: str, on: bool) -> Extension:
    """Turn one on or off, in the user's config rather than in any manifest."""
    ext = find(name)
    if not ext:
        raise LookupError(f"no extension named {name!r} in {BUNDLED_ROOT} "
                          f"or {config.EXTENSIONS}")
    config.set_user(f"extensions.{ext.name}.enabled", on)
    return ext


def collectors() -> dict[str, tuple[pathlib.Path, Extension]]:
    """Collector name -> (file, extension), for every enabled extension."""
    out: dict[str, tuple[pathlib.Path, Extension]] = {}
    for ext in enabled():
        for name, path in ext.collectors().items():
            out[name] = (path, ext)
    return out


def cycles() -> dict[str, tuple[pathlib.Path, Extension]]:
    out: dict[str, tuple[pathlib.Path, Extension]] = {}
    for ext in enabled():
        for name, path in ext.cycles().items():
            out[name] = (path, ext)
    return out


def lib_paths() -> list[str]:
    """Directories to put on a child process's PYTHONPATH."""
    return [str(ext.path / "lib") for ext in enabled() if (ext.path / "lib").is_dir()]


def test_paths() -> list[pathlib.Path]:
    """Test directories to run.

    Every enabled extension, plus every *bundled* one whether enabled or not.
    A bundled extension is Herald's own code and ships to everyone, so its
    tests belong in the suite regardless of whether this particular install has
    turned it on -- otherwise the iMessage parsing, which is the fiddliest code
    in the repository, is only ever tested on a Mac.
    """
    out = []
    for ext in all_extensions():
        if not (ext.enabled or ext.bundled):
            continue
        path = ext.path / "tests"
        if path.is_dir():
            out.append(path)
    return out


# --------------------------------------------------------------------------
# Making the enabled set real: skills a session can load, hooks Claude Code
# runs, units systemd starts. `sync()` is idempotent and is what `herald ext
# enable/disable` calls.
# --------------------------------------------------------------------------

def _merge_hooks(base: dict, extra: dict) -> dict:
    """Claude Code's hooks are {event: [matcher blocks]}, so merging is
    concatenation per event rather than replacement -- two extensions both
    wanting a SessionStart hook is normal, and the second must not silently
    win."""
    out = json.loads(json.dumps(base))
    hooks = out.setdefault("hooks", {})
    for event, blocks in (extra.get("hooks") or {}).items():
        hooks.setdefault(event, []).extend(blocks)
    return out


def _link_skills(link_dir: pathlib.Path, wanted: dict[str, pathlib.Path],
                 report: dict) -> None:
    """Make `link_dir` hold exactly one symlink per wanted skill.

    Only links Herald made are touched: anything pointing outside the
    extensions directory and the program's own skills/ was put there by hand
    and is not ours to remove.
    """
    link_dir.mkdir(parents=True, exist_ok=True)
    for link in link_dir.iterdir():
        if not link.is_symlink():
            continue
        target = pathlib.Path.readlink(link)
        managed = (str(config.EXTENSIONS) in str(target)
                   or str(config.ROOT / "skills") in str(target)
                   or str(target).startswith("../../skills"))
        if not managed:
            continue
        if link.name not in wanted or pathlib.Path(target) != wanted[link.name]:
            link.unlink()
            report["skills_removed"].append(link.name)

    for name, path in wanted.items():
        link = link_dir / name
        if link.exists() or link.is_symlink():
            continue
        link.symlink_to(path)
        report["skills_linked"].append(name)


#: Marks a launcher Herald wrote, so sync never touches one a person made.
LAUNCHER_MARKER = "# herald-extension-launcher"


def _bin_dir() -> pathlib.Path:
    return pathlib.Path(config.get("extensions.bin_dir") or "~/.local/bin").expanduser()


def _launcher(script: pathlib.Path) -> str:
    """A two-line wrapper that runs one extension command with Herald's venv,
    so `herald-transit` works from any shell and any session."""
    try:
        first = script.read_text(errors="replace").splitlines()[0]
    except (OSError, IndexError):
        first = ""
    run = (f'"{config.python()}" "{script}"' if "python" in first else f'"{script}"')
    return f'#!/bin/sh\n{LAUNCHER_MARKER}\nexec {run} "$@"\n'


def _link_commands(report: dict) -> None:
    """Make the bin directory hold one launcher per enabled extension command.

    Every executable in an enabled extension's bin/ gets one, named as the
    script is. A file already there that Herald did not write is left alone
    and reported, and launchers for a disabled extension are removed.
    """
    target = _bin_dir()
    wanted: dict[str, pathlib.Path] = {}
    for ext in enabled():
        d = ext.dir("bin")
        if not d:
            continue
        for script in sorted(d.iterdir()):
            if script.is_file() and script.stat().st_mode & 0o111:
                wanted[script.name] = script
    if target.is_dir():
        for path in target.iterdir():
            if (path.is_file() and not path.is_symlink() and path.name not in wanted
                    and LAUNCHER_MARKER in path.read_text(errors="replace")[:200]):
                path.unlink()
                report["commands_removed"].append(path.name)
    if not wanted:
        return
    target.mkdir(parents=True, exist_ok=True)
    for name, script in wanted.items():
        path = target / name
        text = _launcher(script)
        if path.exists() or path.is_symlink():
            mine = (path.is_file() and not path.is_symlink()
                    and LAUNCHER_MARKER in path.read_text(errors="replace")[:200])
            if not mine:
                report["commands_skipped"].append(name)
                continue
            if path.read_text() == text:
                continue
        path.write_text(text)
        path.chmod(0o755)
        report["commands_linked"].append(name)


def sync() -> dict:
    """Link skills, write settings, and report what changed.

    Everything is done twice, once per engine, from one source of truth: the
    skills are linked into `.claude/skills/` for Claude Code and
    `.agents/skills/` for Codex, and the hooks are written to
    `.claude/settings.json` and `.codex/hooks.json`. Codex's copy has the
    project directory spelled out because Codex does not set
    `$CLAUDE_PROJECT_DIR` -- which is also why that file is gitignored.

    Systemd units are deliberately not installed here: that needs
    `tools/install-units.sh`, which is the one place that knows about
    daemon-reload and where units live on this platform.
    """
    report = {"skills_linked": [], "skills_removed": [], "hook_events": [],
              "skills_skipped": [], "commands_linked": [], "commands_removed": [],
              "commands_skipped": []}

    wanted: dict[str, pathlib.Path] = {}
    # Herald's own skills first, then the extensions'. A skill the user already
    # has at their own scope wins: they may be using it in other projects, and
    # two skills with the same name is worse than one that is slightly out of
    # date.
    user_scope = pathlib.Path.home() / ".claude" / "skills"
    for skill in sorted((config.ROOT / "skills").glob("*/")):
        if not (skill / "SKILL.md").exists():
            continue
        if (user_scope / skill.name).exists():
            report["skills_skipped"].append(skill.name)
            continue
        wanted[skill.name] = skill.resolve()
    for ext in enabled():
        for skill in ext.skills():
            wanted[skill.name] = skill

    _link_skills(SKILLS_DIR, wanted, report)
    _link_commands(report)
    codex_report = {"skills_linked": [], "skills_removed": []}
    _link_skills(CODEX_SKILLS_DIR, wanted, codex_report)

    settings = json.loads(SETTINGS_BASE.read_text()) if SETTINGS_BASE.exists() else {}
    settings["_generated"] = ("written by `herald ext sync` from "
                              "config/claude-settings.base.json plus every enabled "
                              "extension's hooks/hooks.json -- edit those, not this")
    for ext in enabled():
        settings = _merge_hooks(settings, ext.hooks())
    # $HERALD_HOME is where every session keeps its memory, and the repo only
    # reaches it through the `ledger` symlink. Claude Code resolves symlinks
    # before its permission check, so without this a ledger write counts as
    # outside the project: a prompt in an interactive or Remote Control
    # session, a silent denial in a headless one (think.py also passes it as
    # --add-dir for that case). Written here because this file is per install
    # and gitignored, and the path is this install's own.
    perms = settings.setdefault("permissions", {})
    extra = perms.setdefault("additionalDirectories", [])
    home = str(config.HOME.resolve())
    if home not in extra:
        extra.append(home)
    report["hook_events"] = sorted((settings.get("hooks") or {}).keys())
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")

    # Codex parses this file strictly -- `description` and `hooks` are the
    # only keys it accepts, and an unknown one makes it drop the whole file
    # with a warning the model never sees. Found the hard way on 22 Sep 2026:
    # a `_generated` note here meant the guard silently did not run.
    codex_hooks = {"description": settings["_generated"] + "; the Codex copy of "
                                  ".claude/settings.json's hooks",
                   "hooks": settings.get("hooks") or {}}
    codex_text = json.dumps(codex_hooks, indent=2).replace(
        "$CLAUDE_PROJECT_DIR", str(config.ROOT))
    CODEX_HOOKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CODEX_HOOKS_PATH.write_text(codex_text + "\n")
    return report


# --------------------------------------------------------------------------
# Sharing: an extension published as its own git repository is installed by
# cloning it and kept current by fast-forwarding it. That only works while
# nothing personal is written into the clone, which is the same rule a
# tracking install of Herald lives by: the person's values go in their
# config.json (declared in the manifest under a capability's `settings`) or
# in their ledger, and the code stays exactly what upstream shipped.
# --------------------------------------------------------------------------

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


def _ignore_in_home(name: str) -> None:
    """Keep an installed extension out of the ledger's own repository.

    `git add -A` in $HERALD_HOME would otherwise record the clone as an
    embedded repository (a bare gitlink with no URL), which restores as an
    empty directory. Its origin is kept in config instead, where a restore can
    find it.
    """
    if not (config.HOME / ".git").exists():
        return
    ignore = config.HOME / ".gitignore"
    line = f"/extensions/{name}/"
    text = ignore.read_text() if ignore.exists() else ""
    if line in text.splitlines():
        return
    if text and not text.endswith("\n"):
        text += "\n"
    text += (f"# Installed from its own repository; `extensions.{name}.source` "
             f"in config.json says where.\n{line}\n")
    ignore.write_text(text)


def install(url: str, *, enable: bool = False) -> Extension:
    """Clone an extension's repository into $HERALD_HOME/extensions.

    It is left **off** unless `enable` is given. A user extension's manifest
    usually says `"enabled": true`, and a collector is live the moment its
    file exists, so without this the first run of somebody else's code would
    be the timer's, before anyone had read what it does.
    """
    config.EXTENSIONS.mkdir(parents=True, exist_ok=True)
    staging = config.EXTENSIONS / f".installing-{uuid.uuid4().hex[:8]}"
    r = subprocess.run(["git", "clone", "-q", "--", url, str(staging)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(f"git clone failed: {r.stderr.strip() or r.stdout.strip()}")
    try:
        ext = _load(staging)
        if ext is None:
            raise RuntimeError(f"{url} has no {MANIFEST} at its top level, so it "
                               f"is not a Herald extension")
        if "error" in ext.manifest:
            raise RuntimeError(f"its manifest does not parse: {ext.manifest['error']}")
        if not NAME_RE.match(ext.name):
            raise RuntimeError(f"its manifest names it {ext.name!r}, which is not "
                               f"a usable directory name")
        target = config.EXTENSIONS / ext.name
        if target.exists():
            raise FileExistsError(f"{target} already exists; `herald ext update "
                                  f"{ext.name}` updates an installed one")
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    staging.rename(target)
    _ignore_in_home(ext.name)
    apply_defaults(_load(target))
    config.set_user(f"extensions.{ext.name}.source", url)
    config.set_user(f"extensions.{ext.name}.enabled", bool(enable))
    if enable:
        sync()
    return _load(target)


def update(name: str) -> dict:
    """Fast-forward one installed extension to its origin.

    Refuses rather than merges when the clone has changed: a local edit is
    either a personal value that belongs in config, or a fix that belongs
    upstream, and in neither case should an update quietly fold it in.
    """
    ext = find(name)
    if not ext:
        raise LookupError(f"no extension named {name!r}")
    if ext.bundled:
        return {"name": ext.name, "status": "bundled",
                "detail": "ships with Herald and updates with it"}
    if not ext.own_repo():
        return {"name": ext.name, "status": "local",
                "detail": "not its own repository, so there is nothing to update from"}
    dirty = [l for l in _git(ext.path, "status", "--porcelain",
                             "--untracked-files=no").stdout.splitlines() if l.strip()]
    if dirty:
        return {"name": ext.name, "status": "changed",
                "detail": f"{len(dirty)} file(s) edited here; move personal values "
                          f"to config.json, or commit the fix upstream"}
    before = _git(ext.path, "rev-parse", "HEAD").stdout.strip()
    r = _git(ext.path, "pull", "-q", "--ff-only")
    if r.returncode != 0:
        said = (r.stderr.strip() or r.stdout.strip() or "git pull failed").splitlines()
        return {"name": ext.name, "status": "failed", "detail": said[-1]}
    after = _git(ext.path, "rev-parse", "HEAD").stdout.strip()
    if before == after:
        return {"name": ext.name, "status": "current", "detail": before[:7]}
    log = _git(ext.path, "log", "--oneline", f"{before}..{after}").stdout.splitlines()
    apply_defaults(find(name))
    if find(name).enabled:
        sync()
    return {"name": ext.name, "status": "updated",
            "detail": f"{before[:7]} -> {after[:7]}", "log": log}


def publish_problems(name: str) -> tuple[list[str], list[str]]:
    """(problems, notes) for publishing one extension.

    A problem is something that would put this person in public or leave
    another user unable to set it up. The leak scan is the same one `herald
    check` runs on the program, against this install's own name, addresses,
    home paths and secrets.
    """
    from . import leaks  # noqa: PLC0415  (imports config-derived needles)

    ext = find(name)
    if not ext:
        raise LookupError(f"no extension named {name!r}")
    problems, notes = [], []
    for key in ("name", "description", "version"):
        if not ext.manifest.get(key):
            problems.append(f"the manifest has no {key}")
    # Whoever installs it is asked these by Herald, in chat. A manifest with
    # no `setup` (even an empty one, for an extension that needs nothing)
    # has not said what it needs, and a bare key is not a question.
    if "setup" not in ext.manifest:
        problems.append("the manifest has no `setup` list: describe each setting "
                        "it needs, or give `\"setup\": []` if it needs none")
    for item in ext.setup_items():
        if item["prompt"] == item["key"]:
            problems.append(f"{item['key']} has no prompt in `setup`, so the "
                            f"user would be asked for a bare key")
    if not (ext.path / "README.md").exists():
        problems.append("no README.md: someone installing it has nothing to read first")
    if ext.own_repo():
        files = _git(ext.path, "ls-files").stdout.split()
        urls = leaks.remote_urls(ext.path)
        if not ext.source():
            notes.append("its own repository, with no origin yet")
    else:
        skip = {".git", "__pycache__", "venv", ".venv", "node_modules"}
        files = [str(p.relative_to(ext.path)) for p in sorted(ext.path.rglob("*"))
                 if (p.is_file() or p.is_symlink())
                 and not skip & set(p.relative_to(ext.path).parts)
                 and p.suffix != ".pyc"]
        urls = set()
        notes.append("not its own repository yet: `git init` it and add it to "
                     "$HERALD_HOME/.gitignore before publishing")
    problems += leaks.scan(ext.path, files, urls=urls)
    caps = [c for c in ext.manifest.get("provides_capabilities", []) or []
            if isinstance(c, dict)]
    declared = {p for c in caps for p in c.get("settings", ())}
    notes.append(f"{len(files)} file(s) scanned; settings declared: "
                 f"{', '.join(sorted(declared)) or 'none'}")
    return problems, notes


SCAFFOLD_MANIFEST = {
    "name": "",
    "description": "",
    "version": "0.1.0",
    "enabled": True,
    "provides_capabilities": [],
    "requires_capabilities": [],
}


def scaffold(name: str) -> pathlib.Path:
    """Create an empty extension. Directories only where they earn their place:
    a manifest and a collectors directory, because that is what almost every
    extension turns out to be."""
    path = config.EXTENSIONS / name
    if path.exists():
        raise FileExistsError(path)
    (path / "collectors").mkdir(parents=True)
    manifest = dict(SCAFFOLD_MANIFEST, name=name,
                    description=f"{name}: one paragraph on what this reads and why "
                                f"it is worth having.")
    (path / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
    (path / "README.md").write_text(
        f"# {name}\n\nA Herald extension. Its collectors run on the same timer and "
        f"under the same rules as the built-in ones:\n\n"
        f"- no model calls in a collector, ever\n"
        f"- upsert what happened, rebuild what it means\n"
        f"- every write outside the ledger goes through `herald.gwrite`\n\n"
        f"`herald check` holds this code to those rules exactly as it holds "
        f"Herald's own.\n")
    return path
