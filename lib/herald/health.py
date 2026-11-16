"""Is this Herald working right now? A fast, non-interactive answer.

`herald doctor` is for a person: it explains every failure and how to fix it,
and it may take as long as it needs. This is for a machine, mostly Docker's
HEALTHCHECK, which runs every minute and kills the check after ten seconds.
So it has to be quick, read-only, and say in one line per check what is wrong.

A check is either fatal (the container is not doing its job, so Docker should
say "unhealthy") or a warning (something the next digest or `herald doctor`
should mention, but Herald is still running). Before setup has switched the
jobs on, an idle container is healthy: it is doing exactly what it should.

The engine login check runs a vendor CLI, which can take seconds, so its
result is cached for LOGIN_CACHE_SECONDS.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import sqlite3
import time
from dataclasses import asdict, dataclass

from herald import config

LOGIN_CACHE_SECONDS = 15 * 60
SUPERVISOR_STALE = 120          # matches bin/herald-supervisor
CRASH_LOOP_STARTS = 4           # this many starts of one kept-alive job...
CRASH_LOOP_WINDOW = 10 * 60     # ...within this many seconds is a crash loop
COLLECT_STALE_HOURS = 3         # the collect job runs every 30 minutes
MIN_FREE_MB = 500
REQUIRED_TABLES = ("facts", "runs", "commitments", "collector_state", "notifications")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fatal: bool = True


def _supervisor(container: bool) -> tuple[list[Check], bool]:
    """The scheduler's heartbeat and its kept-alive jobs. Returns the checks
    and whether jobs are switched on."""
    from setup import services  # noqa: PLC0415
    if not container:
        return [], True
    try:
        state = json.loads(services.supervisor_state_path().read_text())
    except (OSError, ValueError):
        return [Check("supervisor", False, "no heartbeat file; the entrypoint is not running")], False
    age = time.time() - state.get("ts", 0)
    if age >= SUPERVISOR_STALE:
        return [Check("supervisor", False, f"last heartbeat {int(age)}s ago")], False
    if not state.get("enabled"):
        return [Check("supervisor", True, "idle until setup's last step (`herald setup --web`)")], False
    out = [Check("supervisor", True, f"ticking, {len(state.get('jobs') or {})} jobs")]
    jobs = services.container_jobs()
    now = time.time()
    for label, row in (state.get("jobs") or {}).items():
        if not (jobs.get(label) or {}).get("KeepAlive"):
            continue
        short = label.rsplit(".", 1)[-1]
        recent = [t for t in row.get("starts") or [] if now - t < CRASH_LOOP_WINDOW]
        if len(recent) >= CRASH_LOOP_STARTS:
            out.append(Check(short, False, f"restarted {len(recent)} times in "
                             f"{CRASH_LOOP_WINDOW // 60} minutes; see logs/{label}.log"))
        elif row.get("state") != "active" and now - (row.get("last_start") or 0) > 60:
            out.append(Check(short, False, f"not running (last exit {row.get('last_exit')})"))
        else:
            out.append(Check(short, True, "running"))
    return out, True


def _files(enabled: bool) -> list[Check]:
    """Config and ledger. Before setup has switched the jobs on they may not
    exist yet, which is expected, so their absence only warns."""
    out = []
    for path in (config.CONFIG_PATH, config.LEDGER):
        out.append(Check(path.name, path.exists(),
                         "" if path.exists() else f"missing: {path}"
                         + ("" if enabled else " (setup not run yet)"), fatal=enabled))
    free = shutil.disk_usage(config.HOME).free // (1024 * 1024)
    out.append(Check("disk", free >= MIN_FREE_MB, f"{free} MB free"))
    return out


def _database(enabled: bool) -> list[Check]:
    if not config.DB_PATH.exists():
        return [Check("database", not enabled, "not created yet")]
    try:
        con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=3)
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [t for t in REQUIRED_TABLES if t not in tables]
            out = [Check("database", not missing,
                         "opens, schema present" if not missing else "missing tables: " + ", ".join(missing))]
            if enabled and not missing:
                out += _collectors(con)
        finally:
            con.close()
    except sqlite3.Error as exc:
        out = [Check("database", False, str(exc))]
    return out


def _collectors(con: sqlite3.Connection) -> list[Check]:
    rows = con.execute("SELECT collector, last_ok, consecutive_failures FROM collector_state").fetchall()
    if not rows:
        return [Check("collectors", True, "none has run yet", fatal=False)]
    newest = None
    for _, last_ok, _ in rows:
        try:
            t = dt.datetime.fromisoformat(last_ok) if last_ok else None
        except ValueError:
            t = None
        if t and t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)   # older rows were naive UTC
        if t and (newest is None or t > newest):
            newest = t
    out = []
    if newest is None:
        out.append(Check("collectors", False, "none has ever succeeded"))
    else:
        hours = (dt.datetime.now(dt.timezone.utc) - newest).total_seconds() / 3600
        out.append(Check("collectors", hours < COLLECT_STALE_HOURS,
                         f"last success {hours:.1f}h ago"))
    failing = sorted(r[0] for r in rows if (r[2] or 0) >= 3)
    if failing:
        out.append(Check("failing sources", False, ", ".join(failing), fatal=False))
    return out


def _logins(use_cache: bool) -> list[Check]:
    cache = config.LOGS / "health-logins.json"
    if use_cache:
        try:
            saved = json.loads(cache.read_text())
            if time.time() - saved["ts"] < LOGIN_CACHE_SECONDS:
                return [Check(**c) for c in saved["checks"]]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    from setup.preflight import provider_checks  # noqa: PLC0415
    env = config.agent_env()
    leaked = any(k in env for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY"))
    out = [Check("no API key in agent env", not leaked,
                 "an API key would move Herald onto metered billing" if leaked else "none set")]
    for row in provider_checks():
        out.append(Check(row["name"], row["ok"], row["detail"] if row["ok"] else row["fix"]))
    from herald import google  # noqa: PLC0415
    out.append(Check("google token", google.available(),
                     "present" if google.available() else "`herald setup --step google`", fatal=False))
    try:
        config.LOGS.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"ts": time.time(), "checks": [asdict(c) for c in out]}))
    except OSError:
        pass
    return out


def run(*, container: bool | None = None, use_cache: bool = True) -> list[Check]:
    """Every check, in the order a person would want to read them."""
    import os  # noqa: PLC0415
    import sys  # noqa: PLC0415
    if str(config.ROOT) not in sys.path:
        sys.path.insert(0, str(config.ROOT))     # for `setup.*`
    if container is None:
        container = os.environ.get("HERALD_CONTAINER") == "1"
    checks, enabled = _supervisor(container)
    checks += _files(enabled)
    checks += _database(enabled)
    if enabled:
        checks += _logins(use_cache)
    return checks


def healthy(checks: list[Check]) -> bool:
    return all(c.ok or not c.fatal for c in checks)


def report(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        mark = "ok  " if c.ok else ("FAIL" if c.fatal else "warn")
        lines.append(f"{mark}  {c.name}{'  ' + c.detail if c.detail else ''}")
    lines.append("healthy" if healthy(checks) else "unhealthy")
    return "\n".join(lines)
