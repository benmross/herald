"""Step: extensions other people have published.

The last thing setup offers, after Herald is running, because none of it is
needed and most of it is specific: one university's events, one city's buses.
The "What else should it read?" step already covers the extensions that ship
with Herald; this one covers the published ones in config/extension-catalog.json.

Choosing one clones it and leaves it off, exactly as `herald ext install` does.
Its settings are asked in conversation afterwards, because each extension asks
different questions and some need a login only the person can do.
"""

from __future__ import annotations

from herald import extensions

from .engine import DONE, Field, Outcome, Prompt, State, Step


def status(state: State) -> tuple[str, str]:
    installed = [e["name"] for e in extensions.catalog() if e["installed"]]
    if state.step("extensions").get("seen"):
        return DONE, ", ".join(installed) if installed else "none added"
    return "todo", ""


def prompt(state: State) -> Prompt:
    fields = []
    for item in extensions.catalog():
        if item["installed"]:
            continue
        fields.append(Field(key=f"cat_{item['name']}", type="bool", default=False,
                            label=f"{item['title']}  ({item['for']})",
                            help=f"{item['summary']} [Source]({item['url']})"))
    installed = [e["title"] for e in extensions.catalog() if e["installed"]]
    blurb = ("Optional add-ons other people have published. Each is somebody's "
             "own code that will run on this computer, so add only what you will "
             "use. Nothing turns on yet: once it is downloaded, your agent asks "
             "for its settings in chat.")
    if installed:
        blurb += "\n\nAlready added: " + ", ".join(installed) + "."
    if not fields:
        blurb += "\n\nYou have every extension in the list. More are tagged "\
                 "[herald-extension](https://github.com/topics/herald-extension) on GitHub."
    return Prompt(title="Extensions", blurb=blurb, fields=fields,
                  action="Add these" if fields else "Continue",
                  footnote="More: https://github.com/topics/herald-extension")


def apply(state: State, answers: dict) -> Outcome:
    added, failed = [], []
    for item in extensions.catalog():
        if item["installed"] or not answers.get(f"cat_{item['name']}"):
            continue
        try:
            extensions.install(item["url"])
            added.append(item["name"])
        except Exception as exc:  # noqa: BLE001 -- one bad clone must not stop the rest
            failed.append(f"{item['name']}: {exc}")
    state.step("extensions")["seen"] = True
    if not added:
        return Outcome(ok=not failed, message="No extensions added.", warnings=failed)
    names = ", ".join(added)
    return Outcome(ok=True, warnings=failed,
                   message=f"Downloaded {names}, still off. Message your agent "
                           f"\"set up {added[0]}\" and it will ask for what it needs.")


STEP = Step(key="extensions", title="Extensions", optional=True,
            summary="Add-ons other people have published",
            status_fn=status, prompt_fn=prompt, apply_fn=apply)
