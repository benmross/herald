"""Keep upgraded installs working as core stops carrying one person's defaults.

Two things left core in this release, and both would silently change an
existing install without this:

- `jobs.home_region` used to fall back to a Washington, D.C. pattern. New
  installs now have no home region until they set one. An install upgrading
  from before keeps the D.C. pattern and its "DMV" label, because it has been
  ranking internships by it.
- `herald cf` moved into the bundled cloudflare extension, which is off by
  default. An install that has a Cloudflare token stored has been using it,
  so the extension is switched on there.

Only upgrades run this; fresh installs stamp it. Values already set are kept.
"""
from __future__ import annotations

import importlib.util
import json

from herald import config

DESCRIPTION = "keep the old internship home region and herald cf on upgraded installs"


def _dmv_pattern() -> str:
    spec = importlib.util.spec_from_file_location(
        "internships_for_migration", config.ROOT / "collectors" / "internships.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DMV_PATTERN


def apply(con) -> str:
    path = config.CONFIG_PATH
    if not path.exists():
        return "no existing configuration"
    data = json.loads(path.read_text())
    done = []
    jobs = data.setdefault("jobs", {})
    if "home_region" not in jobs:
        jobs["home_region"] = _dmv_pattern()
        jobs.setdefault("home_region_label", "DMV")
        done.append("kept the D.C.-area internship region")
    token_path = (data.get("cloudflare") or {}).get("token_secret") or "cloudflare.api_token"
    ext = data.setdefault("extensions", {}).setdefault("cloudflare", {})
    if "enabled" not in ext and config.secret(token_path):
        ext["enabled"] = True
        done.append("switched on the cloudflare extension (a token is stored)")
    if not done:
        return "nothing to keep"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)
    config.reload()
    return "; ".join(done)
