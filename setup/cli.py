"""The setup wizard in a terminal.

Renders whatever `engine.py` says to ask and sends the answers back. It decides
nothing: every validation, every verification and every side effect lives in the
steps, so the terminal and the browser cannot drift apart.

Two things it does that the browser does not. A long free-write is unbearable to
type into a terminal prompt, so a textarea opens `$EDITOR` on a real file --
which is also what a person comfortable in a terminal would have wanted anyway.
And the Google sign-in URL is printed rather than linked, with the SSH
port-forward spelled out, because the machine running this is often not the
machine the person is sitting at.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import tempfile
import textwrap

from . import engine
from .engine import BLOCKED, DONE, PARTIAL, TODO, Outcome, State

BOLD, DIM, OFF = "\033[1m", "\033[2m", "\033[0m"
GREEN, YELLOW, RED, BLUE = "\033[32m", "\033[33m", "\033[31m", "\033[34m"


def _tty() -> bool:
    return sys.stdout.isatty()


def _c(text: str, colour: str) -> str:
    return f"{colour}{text}{OFF}" if _tty() else text


def _md(text: str) -> str:
    """Enough Markdown for a terminal: bold, code, bullets, links, headings."""
    out = []
    for para in text.split("\n\n"):
        para = para.rstrip()
        if not para:
            continue
        # Links: "[label](url)" -> "label (url)"; bare <url> -> url
        para = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 → \2", para)
        para = re.sub(r"<(https?://[^>]+)>", r"\1", para)
        para = para.replace("**", BOLD_MARK if _tty() else "")
        if _tty():
            para = re.sub(re.escape(BOLD_MARK) + r"(.*?)" + re.escape(BOLD_MARK),
                          lambda m: f"{BOLD}{m.group(1)}{OFF}", para, flags=re.S)
            para = para.replace(BOLD_MARK, "")
            para = re.sub(r"`([^`]+)`", lambda m: f"{BLUE}{m.group(1)}{OFF}", para)
        else:
            para = para.replace("`", "")
        lines = para.split("\n")
        wrapped = []
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith(("- ", "* ")):
                indent = " " * (len(line) - len(stripped))
                wrapped.append(textwrap.fill(line, width=78,
                                             subsequent_indent=indent + "  "))
            elif stripped.startswith(("#", "    ", "\t")):
                wrapped.append(line)
            else:
                wrapped.append(textwrap.fill(line, width=78))
        out.append("\n".join(wrapped))
    return "\n\n".join(out)


BOLD_MARK = "\x00B\x00"


def _ask_text(field, secret: bool = False) -> str:
    default = str(field.default or "")
    label = field.label or field.key
    if field.help:
        print(_c(textwrap.fill(field.help, 76, initial_indent="  ",
                               subsequent_indent="  "), DIM))
    suffix = f" [{default}]" if default and not secret else ""
    if field.placeholder and not default:
        print(_c(f"  e.g. {field.placeholder}", DIM))
    if secret:
        import getpass
        value = getpass.getpass(f"  {label}: ")
    else:
        value = input(f"  {label}{suffix}: ")
    return value.strip() or default


def _ask_bool(field) -> bool:
    default = bool(field.default)
    if field.help:
        print(_c(textwrap.fill(field.help, 76, initial_indent="  ",
                               subsequent_indent="  "), DIM))
    hint = "Y/n" if default else "y/N"
    answer = input(f"  {field.label} [{hint}]: ").strip().lower()
    if not answer:
        return default
    return answer.startswith("y")


def _ask_choice(field):
    if field.help:
        print(_c(textwrap.fill(field.help, 76, initial_indent="  ",
                               subsequent_indent="  "), DIM))
    print(f"  {field.label}:")
    for i, choice in enumerate(field.choices, 1):
        mark = " (current)" if choice == field.default else ""
        print(f"    {i}. {choice}{mark}")
    while True:
        raw = input(f"  choose 1-{len(field.choices)} [{field.default}]: ").strip()
        if not raw:
            return field.default
        if raw.isdigit() and 1 <= int(raw) <= len(field.choices):
            return field.choices[int(raw) - 1]
        if raw in field.choices:
            return raw
        print(_c("    not one of those", YELLOW))


def _ask_textarea(field) -> str:
    """Open $EDITOR on a real file.

    A thousand words typed into a terminal prompt with no way to go back a line
    is not a thing anyone should be asked to do, and a person in a terminal has
    an editor they like.
    """
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
    if field.help:
        print(_c(textwrap.fill(field.help, 76, initial_indent="  ",
                               subsequent_indent="  "), DIM))
    print(_c(f"  This opens {editor}. Write, save, and close it to continue.", DIM))
    input("  press enter when ready ")
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as fh:
        fh.write(str(field.default or ""))
        path = pathlib.Path(fh.name)
    try:
        subprocess.run([editor, str(path)], check=False)
        return path.read_text().strip()
    finally:
        try:
            path.unlink()
        except OSError:
            pass


class Skip(Exception):
    """The person chose to leave this step for later."""


def ask(prompt) -> dict:
    answers = {}
    for field in prompt.fields:
        print()
        if field.type == "note":
            if field.label:
                print(f"  {_c(field.label, BOLD)}")
            print(_c(textwrap.fill(field.help, 76, initial_indent="  ",
                                   subsequent_indent="  "), DIM))
            continue
        while True:
            if field.type == "bool":
                answers[field.key] = _ask_bool(field)
            elif field.type == "choice":
                answers[field.key] = _ask_choice(field)
            elif field.type == "textarea":
                answers[field.key] = _ask_textarea(field)
            elif field.type == "secret":
                answers[field.key] = _ask_text(field, secret=True)
            else:
                answers[field.key] = _ask_text(field)
            if not field.required or str(answers.get(field.key) or "").strip():
                break
            again = input(_c("  that one is required. Enter to try again, "
                             "or s to skip this step for now: ", YELLOW)).strip().lower()
            if again == "s":
                raise Skip()
    return answers


def _report(outcome: Outcome) -> None:
    mark = _c("✓", GREEN) if outcome.ok else _c("✗", RED)
    print(f"\n{mark} {outcome.message}")
    if outcome.detail:
        print(_c(textwrap.indent(outcome.detail, "  "), DIM))
    for warning in outcome.warnings:
        print(_c(f"  ! {warning}", YELLOW))


STATUS_COLOUR = {DONE: GREEN, TODO: DIM, PARTIAL: YELLOW, BLOCKED: RED}


def show_list(state: State) -> int:
    print(f"\n{_c('herald setup', BOLD)}\n")
    for row in engine.overview(state):
        colour = STATUS_COLOUR.get(row["status"], DIM)
        mark = {DONE: "done", TODO: "  · ", PARTIAL: "part", BLOCKED: "stop"}[row["status"]]
        print(f"  {_c(mark, colour)}  {row['title']:<34} {_c(row['detail'][:120], DIM)}")
    print(f"\n  {_c('herald setup --step <name>', DIM)} runs one step on its own")
    return 0


def run_step(step, state: State) -> bool:
    """One step, start to finish. Returns whether to continue to the next."""
    while True:
        prompt = step.prompt(state)
        print(f"\n{_c('─' * 78, DIM)}")
        print(f"\n{_c(prompt.title, BOLD)}\n")
        if prompt.blurb:
            print(_md(prompt.blurb))
        try:
            answers = ask(prompt) if prompt.fields else {}
            if prompt.immediate or prompt.fields:
                print()
                confirm = input(f"  {prompt.action}: press enter to go on, "
                                f"or s to skip: ").strip().lower()
                if confirm == "s":
                    raise Skip()
        except (Skip, EOFError):
            print(_c("  Skipped. `herald setup` comes back to it later.", DIM))
            return True

        if step.key == "google":
            answers["_on_url"] = _print_google_url
        outcome = step.apply(state, answers)
        _report(outcome)
        if outcome.ok and outcome.more:
            continue
        if not outcome.ok:
            try:
                again = input("\n  Try this step again? [Y/n]: ").strip().lower()
            except EOFError:
                again = "n"
            if again.startswith("n"):
                return True
            continue
        return True


def _print_google_url(url: str) -> None:
    print(f"\n  {_c('Open this in a browser and sign in:', BOLD)}\n")
    print(f"  {url}\n")
    print(_c("  Waiting for you to finish. Google will say the app is not "
             "verified. That is your own app, made minutes ago.", DIM))
    if engine.over_ssh():
        from .google import DEFAULT_PORT  # noqa: PLC0415
        print(_c(f"  You are connected over SSH: the sign-in ends by sending "
                 f"your browser to this machine's port {DEFAULT_PORT}, so the "
                 f"port-forward from the computer you are sitting at has to be "
                 f"running.", DIM))


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="herald setup")
    parser.add_argument("--step", help="run one step on its own")
    parser.add_argument("--list", action="store_true", help="what is done so far")
    parser.add_argument("--web", action="store_true", help="use the browser wizard")
    parser.add_argument("--port", type=int, default=8799)
    args = parser.parse_args(argv)

    state = State()

    if args.web:
        from .web.server import serve  # noqa: PLC0415
        return serve(port=args.port)

    if args.list:
        return show_list(state)

    if args.step:
        step = engine.by_key(args.step)
        if not step:
            print(f"no such step: {args.step}", file=sys.stderr)
            print("have: " + ", ".join(s.key for s in engine.steps()), file=sys.stderr)
            return 2
        run_step(step, state)
        return 0

    print(_md(
        f"\n{'=' * 78}\n\n"
        "**Herald** is a personal agent that reads your mail, your calendars and "
        "whatever else you connect, keeps track of what it concludes, and tells "
        "you each morning what matters.\n\n"
        "This sets it up. Eleven steps, most of them short. The long one is you "
        "writing about yourself, and it is the one that makes the difference. "
        "You can stop at any point and pick up where you left off with "
        "`herald setup`.\n"))

    for step in engine.steps():
        if step.status(state)[0] == DONE:
            continue
        run_step(step, state)

    left = [s for s in engine.steps() if s.status(state)[0] != DONE]
    if not left:
        print(_c("\n  Everything is set up. `herald status` from here.\n", GREEN))
        return 0
    print(_c("\n  That is everything for now. Still to do:", GREEN))
    for s in left:
        print(f"    {s.title:<36} {_c('herald setup --step ' + s.key, DIM)}")
    print(_c("\n  `herald setup` on its own comes back to these.\n", DIM))
    return 0


if __name__ == "__main__":
    sys.exit(main())
