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


GH_NOTE = ("The GitHub tool is installed but not signed in, so the backup to "
           "GitHub is not offered yet. If you would like one: run `gh auth "
           "login` in a terminal window, follow its prompts, then come back to "
           "this step (`herald setup --step home`). Without a backup, this "
           "computer's disk is the only copy of your agent's notes.")


def status(state: State) -> tuple[str, str]:
    home = config.HOME
    if not (home / "config.json").exists():
        return TODO, f"{home} does not exist yet"
    bits = [str(home)]
    if _repo_exists(home):
        remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=home,
                                capture_output=True, text=True).stdout.strip()
        bits.append("with history" + (f", backed up to {remote}" if remote else ", on this computer only"))
    else:
        bits.append("no history kept")
    return DONE, ", ".join(bits)


def prompt(state: State) -> Prompt:
    home = config.HOME
    exists = (home / "config.json").exists()
    versioned = _repo_exists(home)
    has_gh = bool(shutil.which("gh"))
    blurb = (
        f"Herald keeps two things apart. The program is the same for everyone. "
        f"Everything about **you** lives in a folder of your own, `{home}`, "
        f"which is private to this computer.\n\n"
        f"That folder will hold your settings, your sign-in details, and your "
        f"agent's notes: what it has read, what it has concluded, and what it "
        f"has written down about you.\n\n"
        + ("It already exists, so this step leaves it as it is.\n\n" if exists else "")
    )
    fields = []
    if not versioned:
        fields.append(Field(
            key="git", label="Keep a history of changes to it", type="bool", default=True,
            help="So that if your agent writes something wrong about you, the "
                 "earlier version is still there. Nothing leaves this computer."))
    remote = ""
    if versioned:
        remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=home,
                                capture_output=True, text=True).stdout.strip()
    if has_gh and not remote and not _gh_signed_in():
        fields.append(Field(key="note", type="note", label="", help=GH_NOTE))
    elif has_gh and not remote:
        fields.append(Field(
            key="github", label="Also keep a private backup on GitHub",
            type="bool", default=False,
            help="Off unless you say otherwise, and worth a moment's thought. "
                 "The backup would contain what your agent writes about you: "
                 "its notes, its daily journal, and anything personal you tell "
                 "it during setup. It would not contain your sign-in details, "
                 "your mail, your messages, or the raw data it collects. "
                 "Private means only your GitHub account can see it."))
        fields.append(Field(
            key="repo_name", label="Name for the backup", default="herald-ledger",
            help="Only used if you said yes above."))
    elif not has_gh and not remote:
        fields.append(Field(
            key="note", type="note", label="",
            help="If you later want a backup of your agent's notes somewhere "
                 "other than this computer, install the GitHub tool (`gh`) and "
                 "run this step again."))
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
                warnings.append("the history could not be started. Run "
                                "`git -C " + str(home) + " status` to see why.")

    if answers.get("github"):
        name = (answers.get("repo_name") or "herald-ledger").strip()
        if not shutil.which("gh"):
            warnings.append("the GitHub tool is not installed, so no backup was made")
        elif not _gh_signed_in():
            warnings.append("no backup was made. " + GH_NOTE)
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
            summary="Create the private folder that holds everything about you",
            status_fn=status, prompt_fn=prompt, apply_fn=apply)
