"""Deterministic, query-only slash commands supplied by enabled extensions.

Handlers are trusted installed code, not sandboxed plugins. The bridge checks
the sender before invoking one and provides a query-only database connection.
This registry has no access to Telegram approval callbacks.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass
from typing import Callable

from . import extensions


@dataclass(frozen=True)
class Command:
    extension: str
    handler: Callable


_registry: dict[str, Command] | None = None


def word(text: str) -> str:
    first = text.strip().split(None, 1)[0].lower().split("@")[0] if text.strip() else ""
    return first if re.fullmatch(r"/[a-z][a-z0-9_]{0,31}", first) else ""


def commands(reserved=()) -> dict[str, Command]:
    """Load once per bridge process. Conflicting names are refused for everyone."""
    global _registry
    if _registry is None:
        found, conflicts = {}, set()
        for ext in extensions.enabled():
            path = ext.path / "telegram_commands.py"
            if not path.is_file():
                continue
            lib = str(ext.path / "lib")
            if lib not in sys.path:
                sys.path.insert(0, lib)
            try:
                spec = importlib.util.spec_from_file_location(
                    f"herald_ext_telegram_{ext.name}", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                declared = dict(getattr(module, "COMMANDS", {}))
            except Exception as exc:
                print(f"telegram: {ext.name} commands did not load ({type(exc).__name__})",
                      file=sys.stderr)
                continue
            for name, handler in declared.items():
                if not isinstance(name, str) or word(name) != name or not callable(handler):
                    print(f"telegram: invalid command in {ext.name}", file=sys.stderr)
                    continue
                if name in reserved or name in conflicts:
                    print(f"telegram: {ext.name} cannot register reserved command {name}",
                          file=sys.stderr)
                    continue
                if name in found:
                    found.pop(name)
                    conflicts.add(name)
                    print(f"telegram: duplicate command {name}; neither extension may use it",
                          file=sys.stderr)
                    continue
                found[name] = Command(ext.name, handler)
        _registry = found
    return {k: v for k, v in _registry.items() if k not in reserved}
