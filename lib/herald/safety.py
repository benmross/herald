"""Herald's own safety rules, the same for every engine.

Until 4 Oct 2026 the machine-level half of Herald's safety was borrowed from
whichever engine ran the session. Claude Code's auto mode put a classifier in
front of every shell command and blocked exfiltration, `curl | bash`, force
pushes, destroying files that already existed, and edits to its own settings.
Codex had nothing comparable: it has always run with `danger-full-access` and
`approval_policy=never`, with `tools/guard.py` as its only check. So the same
request was refused on one engine and silently allowed on the other, and the
user could not change what either did without editing the engine's own flags.
The day this was written, auto mode refused (correctly, by its own lights) to let
Herald finish a feature its owner had asked for, and the owner switched the
Telegram sessions to `bypassPermissions` -- leaving Claude exactly where Codex
already was.

This module is what replaces the borrowed half. Every rule here is a setting
under `safety.rules` (`block`, `warn` or `off`), applies identically to Claude
and Codex because both run `tools/guard.py` as a PreToolUse hook, and can be
lifted for a few minutes by a tap (`herald safety allow <rule>`) when the user
really did ask for the thing.

The rules, and the failure each one is for:

    egress           data leaving the machine: uploads and POSTs to hosts that
                     are not on the allowlist, paste sites, raw sockets
    remote_exec      running code straight off the network (`curl ... | sh`)
    tunnels          exposing this machine to the internet (ngrok, funnel,
                     `ssh -R`, reverse shells through /dev/tcp)
    secrets          printing or copying credential files
    git_destructive  `reset --hard`, `clean -f`, force push, new remotes:
                     the ways to lose work that nothing else would catch
    protected_paths  deleting, moving or overwriting what already exists under
                     paths the user lists -- an archive with one copy
    self_protect     editing the guard, its hook registration, the engines'
                     settings, or Herald's own safety settings

**It is a tripwire, not a sandbox.** It reads the command the model wrote, and a
model set on getting past it can build the same command at runtime. What it
stops is the accident and the session that was talked into something by text it
read -- which in practice is how damage happens. The tap in red.py and in
`herald root` is the part that cannot be argued with; this is the part that
makes the obvious mistake impossible to make by accident.

**Pure and standard-library only.** `tools/guard.py` loads it by path before
every hooked tool call in every session, so it must import nothing from the
`herald` package (config, db) and must not be slow. The guard hands it the
already-merged settings.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import os
import re
import shlex
from dataclasses import dataclass, field

RULES = ("egress", "remote_exec", "tunnels", "secrets", "git_destructive",
         "protected_paths", "self_protect")
BLOCK, WARN, OFF = "block", "warn", "off"

NET_TOOLS = {"curl", "wget", "http", "https", "httpie", "xh", "aria2c"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "busybox"}
INTERPRETERS = SHELLS | {"python", "python3", "perl", "ruby", "node", "deno", "bun",
                         "php", "lua", "pwsh", "powershell"}
#: commands that only ever name a file, never print or send its contents
HARMLESS_ON_PATH = {"ls", "stat", "test", "[", "[[", "file", "chmod", "chown",
                    "touch", "mkdir", "realpath", "readlink", "dirname", "basename",
                    "du", "wc", "git", "herald"}
PASTE_HOSTS = ("pastebin.com", "paste.rs", "termbin.com", "0x0.st", "transfer.sh",
               "file.io", "ix.io", "sprunge.us", "hastebin.com", "dpaste.org",
               "dpaste.com", "ghostbin.", "paste.ee", "rentry.co", "catbox.moe",
               "bashupload.com", "temp.sh", "pastes.dev", "privatebin.")
TUNNEL_TOOLS = {"ngrok", "cloudflared", "lt", "localtunnel", "bore", "frpc",
                "zrok", "pinggy", "localxpose", "loclx", "tunnelto", "playit"}


@dataclass
class Settings:
    """The `safety` block of the merged config, with paths already expanded."""
    rules: dict = field(default_factory=dict)
    allow_hosts: list = field(default_factory=list)
    protected_paths: list = field(default_factory=list)
    secret_paths: list = field(default_factory=list)
    self_paths: list = field(default_factory=list)
    allowed: set = field(default_factory=set)       # rules lifted by a tap, now

    def mode(self, rule: str) -> str:
        if rule in self.allowed:
            return OFF
        m = (self.rules or {}).get(rule, BLOCK)
        return m if m in (BLOCK, WARN, OFF) else BLOCK


@dataclass
class Finding:
    rule: str
    what: str


# --------------------------------------------------------------------------
# Reading shell. Not a full parser: good enough to find the words that matter,
# and conservative where it cannot tell.
# --------------------------------------------------------------------------

def split_pipelines(text: str) -> list[list[str]]:
    """Pipelines of simple commands, split at unquoted ; & && || newlines and |.

    Quotes are respected so a `-c` script is not cut at its own semicolons.
    `$(...)` and `<(...)` bodies are kept inside the word they belong to and
    examined separately by `nested()`.
    """
    pipelines, pipe, buf = [], [], []
    quote, depth, i = None, 0, 0
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(text):
                buf.append(text[i + 1]); i += 1
            elif ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch; buf.append(ch)
        elif ch == "\\" and i + 1 < len(text):
            buf.append(ch); buf.append(text[i + 1]); i += 1
        elif ch == "(" and i and text[i - 1] in "$<>":
            depth += 1; buf.append(ch)
        elif ch == ")" and depth:
            depth -= 1; buf.append(ch)
        elif depth:
            buf.append(ch)
        elif ch == "|" and text[i + 1:i + 2] != "|":
            pipe.append("".join(buf)); buf = []
        elif ch in ";&\n" or (ch == "|" and text[i + 1:i + 2] == "|"):
            if ch == "&" and text[i + 1:i + 2] == ">":      # &> redirect, not a split
                buf.append(ch); i += 1; continue
            if ch == "&" and i and text[i - 1] in "<>":     # >&2, <&0
                buf.append(ch); i += 1; continue
            pipe.append("".join(buf)); buf = []
            pipelines.append(pipe); pipe = []
            if text[i + 1:i + 2] in ("|", "&") and ch in "|&":
                i += 1
        else:
            buf.append(ch)
        i += 1
    pipe.append("".join(buf))
    pipelines.append(pipe)
    return [[s for s in p if s.strip()] for p in pipelines if any(s.strip() for s in p)]


def nested(text: str) -> list[str]:
    """Bodies of $(...), <(...), >(...) and backticks, one level deep."""
    out = []
    for m in re.finditer(r"[$<>]\(", text):
        start, depth, j = m.end(), 1, m.end()
        while j < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[j], 0)
            j += 1
        out.append(text[start:j - 1])
    out += re.findall(r"`([^`]*)`", text)
    return out


def words(segment: str) -> list[str]:
    try:
        return shlex.split(segment, comments=True, posix=True)
    except ValueError:
        return segment.split()


ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
WRAPPERS = {"sudo", "doas", "env", "nice", "nohup", "time", "timeout", "stdbuf",
            "ionice", "command", "builtin", "exec", "xargs", "watch", "chronic",
            "unbuffer", "setsid", "flock"}


def argv(segment: str) -> list[str]:
    """The words of a simple command, with leading VAR=x and wrappers removed."""
    w = words(segment)
    while w:
        head = os.path.basename(w[0])
        if ASSIGN.match(w[0]):
            w = w[1:]
        elif head in WRAPPERS:
            w = w[1:]
            # drop the wrapper's own options and, for timeout, its duration
            while w and (w[0].startswith("-") or (head == "timeout" and w[0][:1].isdigit())):
                w = w[1:]
        else:
            break
    return w


def prog(w: list[str]) -> str:
    return os.path.basename(w[0]) if w else ""


def inner_scripts(w: list[str]) -> list[str]:
    """Shell handed to a shell: `bash -c '...'`, `eval '...'`, `ssh host '...'`."""
    p = prog(w)
    if p in SHELLS and "-c" in w[1:]:
        k = w.index("-c")
        if k + 1 < len(w):
            return [w[k + 1]]
    if p == "eval" and len(w) > 1:
        return [" ".join(w[1:])]
    return []


def heredocs(command: str) -> list[tuple[str, str]]:
    out = []
    for m in re.finditer(r"([^\n|;&]*?)<<-?\s*(['\"]?)(\w+)\2[^\n]*\n", command):
        lead, tag = m.group(1), m.group(3)
        end = re.search(rf"^\s*{re.escape(tag)}\s*$", command[m.end():], re.M)
        body = command[m.end(): m.end() + end.start()] if end else command[m.end():]
        out.append((lead, body))
    return out


def all_commands(command: str, _depth: int = 0) -> list[list[list[str]]]:
    """Every pipeline in a command line, recursing into -c, eval, $() and
    heredocs fed to a shell. Each pipeline is a list of argv lists."""
    if _depth > 4:
        return []
    outside = command
    extra: list[str] = []
    for lead, body in heredocs(command):
        outside = outside.replace(body, "\n")
        if prog(argv(lead)) in SHELLS:
            extra.append(body)
    result = []
    for pipeline in split_pipelines(outside):
        result.append([argv(seg) for seg in pipeline])
        for seg in pipeline:
            for body in nested(seg):
                extra.append(body)
            for script in inner_scripts(argv(seg)):
                extra.append(script)
    for body in extra:
        result += all_commands(body, _depth + 1)
    return result


# --------------------------------------------------------------------------
# Paths and hosts
# --------------------------------------------------------------------------

def norm(path: str, cwd: str) -> str:
    p = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isabs(p):
        p = os.path.join(cwd, p)
    return os.path.normpath(p)


def under(path: str, roots: list[str]) -> str | None:
    """The root `path` falls under, following symlinks on both sides."""
    cands = {path}
    try:
        cands.add(os.path.realpath(path))
    except OSError:
        pass
    for root in roots:
        for r in {root, os.path.realpath(root)}:
            for c in cands:
                if any(ch in r for ch in "*?["):
                    if fnmatch.fnmatch(c, r) or fnmatch.fnmatch(c, r.rstrip("/") + "/*"):
                        return root
                elif c == r or c.startswith(r.rstrip("/") + "/"):
                    return root
    return None


def path_args(w: list[str], cwd: str) -> list[str]:
    out = []
    for a in w[1:]:
        if a.startswith("-") and "=" in a:
            a = a.split("=", 1)[1]
        elif a.startswith("-"):
            continue
        if a.startswith("@"):                       # curl -d @file
            a = a[1:]
        if "/" in a or a.startswith("~") or a.startswith("."):
            out.append(norm(a, cwd))
        elif a and not a.startswith("$"):
            out.append(norm(a, cwd))
    return out


HOST_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/\s]+@)?([^/:\s?#\]]+|\[[^\]]+\])")


def host_of(arg: str) -> str | None:
    if arg.startswith("-") or arg.startswith("@") or not arg:
        return None
    m = HOST_RE.match(arg.lower())
    if not m:
        return None
    h = m.group(1).strip("[]")
    return h if ("." in h or h == "localhost" or ":" in h) else None


def host_allowed(host: str, allow: list[str]) -> bool:
    host = host.lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(host)
        # Link-local is private by Python's reckoning but includes the cloud
        # metadata endpoint, 169.254.169.254: never a safe destination.
        if ip.is_link_local:
            return False
        if ip.is_loopback or ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10"):
            return True
    except ValueError:
        pass
    if host == "localhost" or host.endswith(".local") or host.endswith(".ts.net"):
        return True
    for pat in allow:
        pat = pat.lower()
        if host == pat or fnmatch.fnmatch(host, pat) or host.endswith("." + pat):
            return True
    return False


# --------------------------------------------------------------------------
# The rules. Each takes the parsed command and returns findings.
# --------------------------------------------------------------------------

def _curl_sends(w: list[str]) -> bool:
    p = prog(w)
    flags = " ".join(a for a in w[1:] if a.startswith("-"))
    if p == "curl":
        if re.search(r"(^|\s)(-d|-F|-T|--data\S*|--form\S*|--upload-file|--json)(\s|=|$)",
                     " " + flags + " "):
            return True
        for i, a in enumerate(w):
            if a in ("-X", "--request") and i + 1 < len(w) and \
                    w[i + 1].upper() in ("POST", "PUT", "PATCH"):
                return True
            if re.match(r"^-[a-zA-Z]*[dFT]$", a) and not a.startswith("--"):
                return True
            if re.match(r"^-X(POST|PUT|PATCH)$", a, re.I):
                return True
    if p == "wget":
        return bool(re.search(r"--(post-data|post-file|body-data|body-file|method=(post|put|patch))",
                              flags, re.I))
    if p in ("http", "https", "httpie", "xh"):
        rest = [a for a in w[1:] if not a.startswith("-")]
        if rest and rest[0].upper() in ("POST", "PUT", "PATCH"):
            return True
        return any(re.match(r"^[^=:/]+(=|:=|@)", a) for a in rest[1:])
    return False


def rule_egress(cmds, cwd, s: Settings) -> list[Finding]:
    out = []
    for pipeline in cmds:
        for w in pipeline:
            p = prog(w)
            hosts = [h for h in (host_of(a) for a in w[1:]) if h]
            if any(any(ph in h for ph in PASTE_HOSTS) for h in hosts):
                out.append(Finding("egress", f"`{p}` to a paste or file-sharing site"))
                continue
            if p in NET_TOOLS and _curl_sends(w):
                bad = [h for h in hosts if not host_allowed(h, s.allow_hosts)]
                if bad or not hosts:
                    out.append(Finding("egress", f"`{p}` sending data to {', '.join(bad) or 'an unknown host'}"))
            elif p in ("nc", "ncat", "netcat", "socat", "telnet"):
                bad = [h for h in hosts if not host_allowed(h, s.allow_hosts)]
                if bad:
                    out.append(Finding("egress", f"raw socket to {', '.join(bad)}"))
            elif p in ("scp", "rsync", "sftp", "rclone"):
                # The destination is the last argument; copying *to* a remote.
                dest = [a for a in w[1:] if not a.startswith("-")]
                if dest and re.match(r"^[^/]*[^\\]:", dest[-1]) and not dest[-1].startswith("/"):
                    if p == "rclone":
                        out.append(Finding("egress", "rclone copy to a remote"))
                    else:
                        h = dest[-1].split(":", 1)[0].split("@")[-1]
                        if not host_allowed(h, s.allow_hosts):
                            out.append(Finding("egress", f"`{p}` copying to {h}"))
            elif p == "gh" and w[1:3] == ["gist", "create"]:
                out.append(Finding("egress", "creating a gist"))
    return out


def rule_remote_exec(cmds, cwd, s: Settings) -> list[Finding]:
    out = []
    for pipeline in cmds:
        fetched = False
        for w in pipeline:
            p = prog(w)
            if p in NET_TOOLS or (p in ("python", "python3") and "urllib" in " ".join(w)):
                fetched = True
            elif fetched and (p in INTERPRETERS or p in ("source", ".")):
                out.append(Finding("remote_exec", f"output of a download piped into `{p}`"))
            # base64 -d | sh: the decoded text is code nobody read
            if (p == "base64" and any(a in ("-d", "--decode", "-D") for a in w)) or \
                    (p == "xxd" and "-r" in w) or (p == "openssl" and "-d" in w):
                fetched = True
            # bash <(curl ...), source <(wget ...), sh -c "$(curl ...)"
            if p in INTERPRETERS | {"source", ".", "eval"}:
                joined = " ".join(w[1:])
                if re.search(r"(<\(|\$\()\s*(curl|wget)\b", joined) or \
                        any(re.match(r"^\s*(curl|wget)\b", b) for b in nested(joined)):
                    out.append(Finding("remote_exec", f"`{p}` running a download"))
    return out


def rule_tunnels(cmds, cwd, s: Settings, raw: str) -> list[Finding]:
    out = []
    if re.search(r"/dev/(tcp|udp)/", raw):
        out.append(Finding("tunnels", "a /dev/tcp socket (the shape of a reverse shell)"))
    for pipeline in cmds:
        for w in pipeline:
            p = prog(w)
            if p in TUNNEL_TOOLS and not (p == "cloudflared" and "tunnel" not in w):
                out.append(Finding("tunnels", f"`{p}` exposes this machine to the internet"))
            elif p == "tailscale" and "funnel" in w:
                out.append(Finding("tunnels", "tailscale funnel publishes to the internet"))
            elif p in ("ssh", "autossh") and any(a == "-R" or a.startswith("-R") for a in w[1:]):
                out.append(Finding("tunnels", "ssh -R opens a port on a remote host into this one"))
            elif p in ("nc", "ncat", "netcat") and any(a in ("-e", "-c") or a.startswith("--exec")
                                                       or a.startswith("--sh-exec") for a in w):
                out.append(Finding("tunnels", f"`{p}` handing a shell to the network"))
            elif p == "socat" and re.search(r"exec:|system:", " ".join(w).lower()):
                out.append(Finding("tunnels", "socat attaching a program to a socket"))
    return out


def rule_secrets(cmds, cwd, s: Settings, raw: str = "") -> list[Finding]:
    out = []
    for m in re.finditer(r"(?<![<])<\s*([^\s;&|<>()]+)", raw):
        tgt = norm(m.group(1).strip("'\""), cwd)
        if under(tgt, s.secret_paths):
            out.append(Finding("secrets", f"reading {tgt}, a credential file, by redirection"))
    for pipeline in cmds:
        for w in pipeline:
            p = prog(w)
            if not w or p in HARMLESS_ON_PATH:
                continue
            for path in path_args(w, cwd):
                hit = under(path, s.secret_paths)
                if hit:
                    out.append(Finding("secrets", f"`{p}` on {path}, a credential file"))
                    break
    return out


def rule_git(cmds, cwd, s: Settings) -> list[Finding]:
    out = []
    for pipeline in cmds:
        for w in pipeline:
            if prog(w) != "git":
                continue
            args = [a for a in w[1:]]
            # skip `git -C dir` and other global options
            while args and args[0].startswith("-"):
                args = args[2:] if args[0] in ("-C", "-c", "--git-dir", "--work-tree") else args[1:]
            if not args:
                continue
            sub, rest = args[0], args[1:]
            if sub == "reset" and "--hard" in rest:
                out.append(Finding("git_destructive", "git reset --hard discards uncommitted work"))
            elif sub == "clean" and any(re.match(r"^-[a-zA-Z]*f", a) or a == "--force" for a in rest):
                out.append(Finding("git_destructive", "git clean -f deletes untracked files"))
            elif sub in ("checkout", "restore") and ("." in rest or "--" in rest and rest[-1:] == ["."]):
                out.append(Finding("git_destructive", f"git {sub} . discards every local change"))
            elif sub == "stash" and rest[:1] in (["drop"], ["clear"]):
                out.append(Finding("git_destructive", f"git stash {rest[0]} deletes stashed work"))
            elif sub == "push" and any(a in ("-f", "--force", "--mirror", "--delete", "-d")
                                       or a.startswith("--force") or a.startswith("+")
                                       or re.match(r"^:", a) for a in rest):
                out.append(Finding("git_destructive", "force push or remote branch deletion"))
            elif sub == "push" and any(re.match(r"^(https?://|git@|ssh://)", a) for a in rest):
                out.append(Finding("git_destructive", "push to a URL rather than a configured remote"))
            elif sub == "remote" and rest[:1] in (["add"], ["set-url"]):
                out.append(Finding("git_destructive", f"git remote {rest[0]} changes where pushes go"))
            elif sub in ("filter-branch", "filter-repo"):
                out.append(Finding("git_destructive", f"git {sub} rewrites history"))
            elif sub == "branch" and any(a in ("-D", "--delete --force") for a in rest):
                out.append(Finding("git_destructive", "git branch -D deletes unmerged work"))
    return out


DELETERS = {"rm", "rmdir", "shred", "unlink", "truncate", "srm", "wipe"}


def _write_targets(w: list[str], segment: str, cwd: str) -> tuple[list[str], list[str]]:
    """(paths this command removes or replaces, paths it writes into)."""
    p = prog(w)
    gone, written = [], []
    if p in DELETERS:
        gone += path_args(w, cwd)
    elif p == "mv":
        paths = path_args(w, cwd)
        if paths:
            gone += paths[:-1]             # sources disappear
            written.append(paths[-1])
    elif p in ("cp", "install", "ln"):
        paths = path_args(w, cwd)
        if paths:
            written.append(paths[-1])
    elif p == "rsync":
        paths = path_args(w, cwd)
        if paths:
            written.append(paths[-1])
            if any(a.startswith("--delete") or a == "--remove-source-files" for a in w):
                gone.append(paths[-1])
    elif p == "find" and ("-delete" in w or any(a in ("rm", "/bin/rm") for a in w)):
        gone += [norm(a, cwd) for a in w[1:2] if not a.startswith("-")] or [cwd]
    elif p == "dd":
        gone += [norm(a[3:], cwd) for a in w if a.startswith("of=")]
    elif p == "sed" and any(a == "-i" or a.startswith("-i") or a.startswith("--in-place") for a in w):
        written += path_args(w, cwd)[1:] if len(path_args(w, cwd)) > 1 else []
    elif p == "tee":
        written += path_args(w, cwd)
    elif p in ("chmod", "chown", "chattr") and w[1:2] and (w[1].startswith("-R") or "R" in w[1][1:2]):
        written += path_args(w, cwd)[1:]
    # redirections: > file, >> file, &> file
    for m in re.finditer(r"(?<![<0-9&])(?:[12&]?>{1,2})\|?\s*([^\s;&|<>]+)", segment):
        tgt = m.group(1).strip("'\"")
        if tgt.startswith("&") or tgt == "/dev/null" or tgt.startswith("/dev/"):
            continue
        written.append(norm(tgt, cwd))
    return gone, written


def _segments(command: str) -> list[str]:
    out = []
    for pipeline in split_pipelines(command):
        out += pipeline
    for lead, body in heredocs(command):
        out.append(lead)
    return out


def rule_paths(command: str, cwd: str, s: Settings, depth: int = 0) -> list[Finding]:
    """protected_paths and self_protect both look at what a command writes."""
    out = []
    if depth > 4:
        return out
    outside = command
    for _, body in heredocs(command):
        outside = outside.replace(body, "\n")
    for seg in _segments(outside):
        w = argv(seg)
        if not w:
            continue
        gone, written = _write_targets(w, seg, cwd)
        for path in gone:
            hit = under(path, s.protected_paths)
            if hit:
                out.append(Finding("protected_paths",
                                   f"`{prog(w)}` would remove or replace {path} (under {hit})"))
        for path in written:
            if os.path.lexists(path):
                hit = under(path, s.protected_paths)
                if hit and not os.path.isdir(path):
                    out.append(Finding("protected_paths",
                                       f"`{prog(w)}` would overwrite {path} (under {hit})"))
        for path in gone + written:
            hit = under(path, s.self_paths)
            if hit:
                out.append(Finding("self_protect", f"`{prog(w)}` would change {path}"))
        for script in inner_scripts(w) + nested(seg):
            out += rule_paths(script, cwd, s, depth + 1)
        # `herald config set safety...` changes the rules themselves
        if prog(w) == "herald" and w[1:3] == ["config", "set"] and \
                len(w) > 3 and w[3].startswith("safety"):
            out.append(Finding("self_protect", "changing Herald's safety settings"))
    return out


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------

def check_bash(command: str, cwd: str, s: Settings) -> list[Finding]:
    cmds = all_commands(command)
    found = []
    found += rule_egress(cmds, cwd, s)
    found += rule_remote_exec(cmds, cwd, s)
    found += rule_tunnels(cmds, cwd, s, command)
    found += rule_secrets(cmds, cwd, s, command)
    found += rule_git(cmds, cwd, s)
    found += rule_paths(command, cwd, s)
    return found


PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.M)


def file_targets(tool: str, tool_input: dict, cwd: str) -> list[str]:
    """Paths a file tool writes, for Claude's Write/Edit and Codex's apply_patch."""
    paths = []
    for key in ("file_path", "path", "notebook_path"):
        if isinstance(tool_input.get(key), str):
            paths.append(norm(tool_input[key], cwd))
    for value in tool_input.values():
        if isinstance(value, str) and "*** " in value:
            paths += [norm(m.strip(), cwd) for m in PATCH_FILE.findall(value)]
    return paths


def check_file_write(tool: str, tool_input: dict, cwd: str, s: Settings) -> list[Finding]:
    out = []
    for path in file_targets(tool, tool_input, cwd):
        hit = under(path, s.self_paths)
        if hit:
            out.append(Finding("self_protect", f"`{tool}` would change {path}"))
        hit = under(path, s.protected_paths)
        if hit and os.path.lexists(path):
            out.append(Finding("protected_paths",
                               f"`{tool}` would overwrite {path} (under {hit})"))
    return out


def check_file_read(tool: str, tool_input: dict, cwd: str, s: Settings) -> list[Finding]:
    for key in ("file_path", "path"):
        v = tool_input.get(key)
        if isinstance(v, str) and under(norm(v, cwd), s.secret_paths):
            return [Finding("secrets", f"`{tool}` on {v}, a credential file")]
    return []


def evaluate(tool: str, tool_input: dict, cwd: str, s: Settings) -> list[Finding]:
    """Every finding for one tool call, before modes are applied."""
    if tool in ("Bash", "shell", "exec_command", "local_shell"):
        cmd = tool_input.get("command")
        if isinstance(cmd, list):
            cmd = " ".join(shlex.quote(c) for c in cmd)
        return check_bash(cmd or "", cwd, s)
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch"):
        return check_file_write(tool, tool_input, cwd, s)
    if tool in ("Read", "NotebookRead"):
        return check_file_read(tool, tool_input, cwd, s)
    return []


def verdict(findings: list[Finding], s: Settings) -> tuple[list[Finding], list[Finding]]:
    """(blocking, warning-only) after each rule's mode is applied."""
    block = [f for f in findings if s.mode(f.rule) == BLOCK]
    warn = [f for f in findings if s.mode(f.rule) == WARN]
    return block, warn


def explain(block: list[Finding]) -> str:
    rules = sorted({f.rule for f in block})
    lines = [f"Blocked by Herald's safety rules ({', '.join(rules)}):"]
    lines += [f"  - {f.what}" for f in block[:5]]
    lines.append(
        "These are Herald's own rules, the same on every engine; see `herald safety "
        "status`. If the user asked for exactly this in this conversation, run "
        f"`herald safety allow {rules[0]} --why '<what they asked for>'`: they get one "
        "tap on their phone, and on yes the rule is lifted for ten minutes. Otherwise "
        "do it another way, or tell them what you wanted to do and why. Do not "
        "rephrase the command to get past this.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Settings, read straight from the config files. The guard cannot import
# herald.config (too heavy for a hook that runs before every call), so this is
# the same merge in miniature: defaults, then the user's config.json on top.
# --------------------------------------------------------------------------

def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _expand(p: str, root: str, home: str) -> str:
    p = p.replace("$HERALD_ROOT", root).replace("$HERALD_HOME", home)
    return os.path.normpath(os.path.expandvars(os.path.expanduser(p)))


def ssh_hosts(path: str = "~/.ssh/config") -> list[str]:
    """Hosts and HostNames in the user's ssh config: machines that are theirs."""
    out = []
    try:
        with open(os.path.expanduser(path)) as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 2 and parts[0].lower() in ("host", "hostname"):
                    out += [h for h in parts[1:] if not any(c in h for c in "*?!")]
    except OSError:
        pass
    return out


def allow_dir(home: str) -> str:
    return os.path.join(home, "run", "safety")


def granted(home: str, now: float | None = None) -> set[str]:
    """Rules lifted by a tap that has not yet run out. `<rule>.allow` holds the
    epoch second it expires; only `herald safety allow` writes one, after the
    user taps yes, and self_protect stops a session writing one itself."""
    import time  # noqa: PLC0415
    now = time.time() if now is None else now
    out = set()
    try:
        names = os.listdir(allow_dir(home))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".allow") or name[:-6] not in RULES:
            continue
        try:
            with open(os.path.join(allow_dir(home), name)) as f:
                if float(f.read().strip()) > now:
                    out.add(name[:-6])
        except (OSError, ValueError):
            continue
    return out


def raw_config(root: str, home: str) -> dict:
    import json  # noqa: PLC0415
    cfg: dict = {}
    for path in (os.path.join(root, "config", "defaults.json"),
                 os.path.join(home, "config.json")):
        try:
            with open(path) as f:
                cfg = _merge(cfg, json.load(f))
        except (OSError, ValueError):
            continue
    return cfg.get("safety") or {}


def load(root: str, home: str) -> Settings:
    raw = raw_config(root, home)
    exp = lambda xs: [_expand(x, root, home) for x in xs or [] if isinstance(x, str)]  # noqa: E731
    return Settings(
        rules={k: v for k, v in (raw.get("rules") or {}).items() if not k.startswith("_")},
        allow_hosts=[h for h in raw.get("allow_hosts") or [] if isinstance(h, str)] + ssh_hosts(),
        protected_paths=exp(raw.get("protected_paths")),
        secret_paths=exp(raw.get("secret_paths")),
        self_paths=exp(raw.get("self_paths")),
        allowed=granted(home),
    )


def engine_mode(surface: str, root: str, home: str) -> str:
    """auto or herald, for one surface (telegram, cycle, think, brain)."""
    raw = raw_config(root, home)
    mode = (raw.get("surfaces") or {}).get(surface) or raw.get("engine_mode") or "auto"
    return mode if mode in ("auto", "herald") else "auto"
