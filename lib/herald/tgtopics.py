"""Preserve topic names while marking the engine selected for each topic."""
from __future__ import annotations

import re

DOTS = {"claude": "🟠", "codex": "🔵", "kiro": "🟣"}
_SUFFIX = re.compile(r"(?:\s*[🟠🔵🟣])+$")


def marked_name(name: str, engine: str) -> str:
    """Telegram limits names to 128 characters, including the marker."""
    base = _SUFFIX.sub("", name).rstrip()
    return base[:126].rstrip() + " " + DOTS[engine]


def observe(topic: dict, message: dict) -> None:
    """Learn names from service events, or the topic's original root reply.

    An old root cannot replace a subsequently observed rename. The Bot API
    cannot list existing topic names, so an unknown name must stay untouched.
    """
    thread_id = message.get("message_thread_id")
    if not thread_id or thread_id == 1:
        return
    edited = message.get("forum_topic_edited") or {}
    created = message.get("forum_topic_created") or {}
    if name := edited.get("name") or created.get("name"):
        topic["topic_name"] = name
        return
    root = message.get("reply_to_message") or {}
    if root.get("message_id") == thread_id:
        name = (root.get("forum_topic_created") or {}).get("name")
        if name:
            topic.setdefault("topic_name", name)


def sync(topic: dict, key: str, api, *, enabled: bool, default_engine: str) -> bool:
    """Edit only a known name; callers keep private topics out of this path."""
    if not enabled or not topic.get("topic_name"):
        return False
    chat, _, thread = key.partition(":")
    if not thread.isdecimal() or int(thread) == 1:
        return False
    engine = topic.get("engine") or default_engine
    if engine not in DOTS:
        return False
    name = marked_name(topic["topic_name"], engine)
    if name == topic["topic_name"]:
        return False
    response = api("editForumTopic", chat_id=int(chat), message_thread_id=int(thread), name=name)
    unchanged = (response or {}).get("error_code") == 400 and any(
        text in (response or {}).get("description", "").lower()
        for text in ("topic_not_modified", "topic is not modified"))
    if (response or {}).get("ok") or unchanged:
        topic["topic_name"] = name
        return True
    return False
