"""Read what a pull request would contain, from a git checkout here or on a device.

Opening a pull request is red: other people see it, and on a public repository
so does everyone else. The push and the `gh pr create` that do it live in
red.py's performer and run only after a tap. This module does the read-only
half: it looks at the checkout and builds the payload the tap is rendered from,
so what the user approves is the branch as it actually is, with its commit
hash, rather than a session's description of it.

The checkout is often not on this machine. Work on someone's laptop is done in
a clone on the laptop, signed in to their own `gh`, so every command can run
over ssh on a configured device. Commands run under the remote user's login
shell, because a non-interactive ssh session on macOS does not have Homebrew on
its PATH and `gh` would not be found.
"""

from __future__ import annotations

import re
import shlex
import subprocess


class PullRequestError(RuntimeError):
    pass


def run(argv: list[str], *, cwd: str, host: str | None = None,
        stdin: str | None = None, timeout: int = 120) -> str:
    """Run one command in the checkout and return its stdout, or raise."""
    if host:
        inner = f"cd {shlex.quote(cwd)} && {shlex.join(argv)}"
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host,
               f'exec "$SHELL" -lc {shlex.quote(inner)}']
        kwargs = {}
    else:
        cmd = argv
        kwargs = {"cwd": cwd}
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                           timeout=timeout, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PullRequestError(f"{argv[0]} did not run: {exc}") from exc
    if r.returncode != 0:
        where = f" on {host}" if host else ""
        raise PullRequestError(
            f"`{shlex.join(argv)}` failed{where}: "
            f"{(r.stderr or r.stdout).strip()[:400]}")
    return r.stdout


def _slug(url: str) -> str | None:
    """owner/repo from a GitHub remote URL, https or ssh."""
    m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def describe(*, cwd: str, repo: str, base: str, title: str, body: str,
             branch: str | None = None, remote: str = "origin",
             host: str | None = None) -> dict:
    """The payload for a `github.pr` approval: exactly what would be done.

    `repo` is the repository the pull request is opened against, `remote` is
    where the branch is pushed (a fork, or the same repository). The commit
    list and the file summary are measured against `repo`'s `base` as fetched
    now, so the user sees what the reviewers will.
    """
    if not title.strip():
        raise PullRequestError("a pull request needs a title")
    branch = branch or run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           cwd=cwd, host=host).strip()
    if branch in ("HEAD", base):
        raise PullRequestError(f"refusing to open a pull request from {branch!r}")
    sha = run(["git", "rev-parse", branch], cwd=cwd, host=host).strip()
    if run(["git", "status", "--porcelain", "--untracked-files=no"],
           cwd=cwd, host=host).strip():
        raise PullRequestError(
            "the checkout has uncommitted changes; commit them or stash them "
            "first, so what is pushed is what was approved")

    remotes: dict[str, str] = {}
    for line in run(["git", "remote", "-v"], cwd=cwd, host=host).splitlines():
        parts = line.split()
        if len(parts) >= 2:
            remotes.setdefault(parts[0], parts[1])
    if remote not in remotes:
        raise PullRequestError(f"no remote named {remote!r}; have {sorted(remotes)}")
    push_slug = _slug(remotes[remote])
    if not push_slug:
        raise PullRequestError(f"remote {remote!r} is not a GitHub repository")
    upstream = next((name for name, url in remotes.items() if _slug(url) == repo), None)
    if upstream is None:
        raise PullRequestError(
            f"no remote points at {repo}; add one so the commits can be "
            f"compared with its {base}")

    run(["git", "fetch", "--quiet", upstream, base], cwd=cwd, host=host)
    ref = f"{upstream}/{base}"
    commits = run(["git", "log", "--format=%h %s", f"{ref}..{sha}"],
                  cwd=cwd, host=host).splitlines()
    if not commits:
        raise PullRequestError(f"{branch} has no commits that {ref} does not")
    files = run(["git", "diff", "--name-status", f"{ref}...{sha}"],
                cwd=cwd, host=host).splitlines()
    stat = (run(["git", "diff", "--shortstat", f"{ref}...{sha}"],
                cwd=cwd, host=host).strip())

    owner = push_slug.split("/")[0]
    return {
        "repo": repo, "base": base,
        "head": branch if push_slug == repo else f"{owner}:{branch}",
        "remote": remote, "push_to": push_slug, "branch": branch, "sha": sha,
        "dir": cwd, "host": host,
        "title": title.strip(), "body": body,
        "commits": commits, "files": files, "stat": stat,
        "target": repo,
    }
