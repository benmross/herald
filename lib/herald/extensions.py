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
      bin/                     long-running surfaces of its own
      systemd/                 units, installed with the built-in ones
      skills/                  symlinked where sessions will load them
      hooks/hooks.json         Claude Code hooks, merged into .claude/settings.json
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
#: Claude Code project settings, generated: base + every enabled extension's hooks.
SETTINGS_PATH = config.ROOT / ".claude" / "settings.json"
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
    return [ext.path / "tests" for ext in enabled() if (ext.path / "tests").is_dir()]


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


def sync() -> dict:
    """Link skills, write settings, and report what changed.

    Systemd units are deliberately not installed here: that needs
    `tools/install-units.sh`, which is the one place that knows about
    daemon-reload and where units live on this platform.
    """
    report = {"skills_linked": [], "skills_removed": [], "hook_events": [],
              "skills_skipped": []}

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
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

    for link in SKILLS_DIR.iterdir():
        if not link.is_symlink():
            continue
        target = pathlib.Path.readlink(link)
        # Manage links into the extensions directory and into the program's own
        # skills/ -- anything else in here was put there by hand and is not
        # ours to remove.
        managed = (str(config.EXTENSIONS) in str(target)
                   or str(config.ROOT / "skills") in str(target)
                   or str(target).startswith("../../skills"))
        if not managed:
            continue
        if link.name not in wanted or pathlib.Path(target) != wanted[link.name]:
            link.unlink()
            report["skills_removed"].append(link.name)

    for name, path in wanted.items():
        link = SKILLS_DIR / name
        if link.exists() or link.is_symlink():
            continue
        link.symlink_to(path)
        report["skills_linked"].append(name)

    settings = json.loads(SETTINGS_BASE.read_text()) if SETTINGS_BASE.exists() else {}
    settings["_generated"] = ("written by `herald ext sync` from "
                              "config/claude-settings.base.json plus every enabled "
                              "extension's hooks/hooks.json -- edit those, not this")
    for ext in enabled():
        settings = _merge_hooks(settings, ext.hooks())
    report["hook_events"] = sorted((settings.get("hooks") or {}).keys())
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")
    return report


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
