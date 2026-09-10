"""Step 2: where this person's Herald keeps everything it knows about them.

The whole point of the split between the program and the person is that this
directory is *theirs*: their config, their credentials, their ledger, their
extensions, in a place a `git pull` of Herald never touches and a public
repository can never contain.

This step creates it, makes it a git repository, and offers to back it up to a
private GitHub repo -- offers, because "my entire life in a file" and "on
somebody else's server" is a decision that belongs to the person, and the
honest version of that offer says exactly what would be in it.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

from herald import config

from .engine import DONE, TODO, Field, Outcome, Prompt, State, Step

GITIGNORE = """\
# Herald's memory, versioned. What is deliberately not:

ledger/facts.db
ledger/facts.db-wal
ledger/facts.db-shm
ledger/raw/
ledger/documents/

# Credentials. Never in git, in any repo, private or not -- a private repo is
# one visibility flip away from a public one.
secrets.json
setup-state.json

__pycache__/
*.pyc
"""

README = """\
# My Herald

Everything my personal agent knows about me.

    config.json      settings
    secrets.json     credentials -- never committed
    ledger/
      identity/      who I am, and the rules this agent follows for me
      state/         what is true right now; the agent rewrites these
      journal/       one file per day, append-only
      digests/       what it sent me each morning
      facts.db       everything it has ingested -- not committed, rebuildable
    extensions/      collectors and skills that are mine rather than Herald's

The program itself lives elsewhere and is public. This is not.
"""


def _repo_exists(path: pathlib.Path) -> bool:
    return (path / ".git").exists()


def _gh_signed_in() -> bool:
    if not shutil.which("gh"):
        return False
    return subprocess.run(["gh", "auth", "status"], capture_output=True,
                          text=True).returncode == 0


GH_NOTE = ("The GitHub CLI is installed but not signed in, so the off-machine "
           "backup is not offered yet. If you want one: run `gh auth login` in "
           "a terminal, follow its prompts, and run this step again "
           "(`herald setup --step home`). Without one, a dead disk takes the "
           "ledger.")


def status(state: State) -> tuple[str, str]:
    home = config.HOME
    if not (home / "config.json").exists():
        return TODO, f"{home} does not exist yet"
    bits = [str(home)]
    if _repo_exists(home):
        remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=home,
                                capture_output=True, text=True).stdout.strip()
        bits.append("versioned" + (f", backed up to {remote}" if remote else ", local only"))
    else:
        bits.append("not versioned")
    return DONE, " — ".join(bits)


def prompt(state: State) -> Prompt:
    home = config.HOME
    exists = (home / "config.json").exists()
    versioned = _repo_exists(home)
    has_gh = bool(shutil.which("gh"))
    blurb = (
        f"Herald keeps two things apart. The program is this checkout, and it is "
        f"the same for everyone. Everything about **you** goes in a directory of "
        f"your own — `{home}` — which nothing public ever touches.\n\n"
        f"That directory will hold your settings, your credentials, and the "
        f"ledger: what your agent has read, what it has concluded, and what it "
        f"has written down about you.\n\n"
        + ("It already exists, so this step will leave it alone.\n\n" if exists else "")
    )
    fields = []
    if not versioned:
        fields.append(Field(
            key="git", label="Keep a version history of it", type="bool", default=True,
            help="A local git repository, so a bad edit to something the agent "
                 "wrote about you can be undone. Nothing leaves the machine."))
    remote = ""
    if versioned:
        remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=home,
                                capture_output=True, text=True).stdout.strip()
    if has_gh and not remote and not _gh_signed_in():
        fields.append(Field(key="note", type="note", label="", help=GH_NOTE))
    elif has_gh and not remote:
        fields.append(Field(
            key="github", label="Also back it up to a private GitHub repository",
            type="bool", default=False,
            help="Off by default, and think about it before you say yes. It "
                 "would contain what the agent writes about you: your notes, "
                 "your daily journal, and anything personal you tell it during "
                 "setup. It would NOT contain your credentials, your mail, your "
                 "messages, or the ingested database — those are never "
                 "committed. Private means private to your GitHub account."))
        fields.append(Field(
            key="repo_name", label="Repository name", default="herald-ledger",
            help="Only used if you said yes above."))
    elif not has_gh and not remote:
        fields.append(Field(
            key="note", type="note", label="",
            help="Install the GitHub CLI (`gh`) if you later want an off-machine "
                 "backup of the ledger. Without one, a dead disk takes it."))
    return Prompt(title="Where your Herald lives", blurb=blurb, fields=fields,
                  action="Create it" if not exists else "Continue",
                  immediate=not fields)


def apply(state: State, answers: dict) -> Outcome:
    home = config.HOME
    config.ensure_dirs()
    (home / "extensions").mkdir(exist_ok=True)
    if not (home / "config.json").exists():
        (home / "config.json").write_text("{}\n")
    if not (home / "secrets.json").exists():
        (home / "secrets.json").write_text("{}\n")
    os.chmod(home / "secrets.json", 0o600)
    if not (home / ".gitignore").exists():
        (home / ".gitignore").write_text(GITIGNORE)
    if not (home / "README.md").exists():
        (home / "README.md").write_text(README)

    warnings = []
    want_git = answers.get("git", True) and not _repo_exists(home)
    if want_git:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=home, check=False)
        # A person who has never used git has no name and email configured,
        # and git refuses to commit without them -- "Please tell me who you
        # are" was the first thing the first outside install saw. Set for
        # this repository only, never globally; ledger.commit passes the same
        # on every later commit too.
        subprocess.run(["git", "config", "user.name",
                        config.get("agent.name", "Herald") or "Herald"], cwd=home, check=False)
        subprocess.run(["git", "config", "user.email", "herald@localhost"],
                       cwd=home, check=False)
        from herald import ledger  # noqa: PLC0415
        if not ledger.commit("My Herald: config, ledger, extensions", push=False):
            r = subprocess.run(["git", "log", "--oneline", "-1"], cwd=home,
                               capture_output=True, text=True)
            if not r.stdout.strip():
                warnings.append("the first commit did not happen; "
                                "`git -C " + str(home) + " status` says why")

    if answers.get("github"):
        name = (answers.get("repo_name") or "herald-ledger").strip()
        if not shutil.which("gh"):
            warnings.append("the GitHub CLI is not installed, so no backup was made")
        elif not _gh_signed_in():
            warnings.append("the GitHub CLI is not signed in, so no backup was "
                            "made. " + GH_NOTE)
        else:
            r = subprocess.run(
                ["gh", "repo", "create", name, "--private", "--source=.",
                 "--remote=origin", "--push"],
                cwd=home, capture_output=True, text=True)
            if r.returncode != 0:
                warnings.append(f"GitHub said: {(r.stderr or r.stdout).strip()[:300]}")
            else:
                state.step("home")["remote"] = name

    state.step("home")["path"] = str(home)
    return Outcome(ok=True, message=f"Your Herald's home is {home}.",
                   warnings=warnings)


STEP = Step(key="home", title="Where your Herald lives",
            summary="Create the private directory that holds everything about you",
            status_fn=status, prompt_fn=prompt, apply_fn=apply)
