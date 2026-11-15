"""Telegram transport: bounded retries and a shared flood-control cooldown.

Keep HTTP failures as structured responses so callers can distinguish an
invalid message from an outage. Never log a request URL: it contains the token.
"""
from __future__ import annotations

import json
import math
import sys
import threading
import time
import urllib.error
import urllib.request

from . import config


def transient(response) -> bool:
    return response is None or response.get("error_code") == 429 or response.get("error_code", 0) >= 500


def retry_after(response) -> float:
    try:
        value = float(((response or {}).get("parameters") or {}).get("retry_after", 0))
        return value if math.isfinite(value) and value > 0 else 0
    except (TypeError, ValueError):
        return 0


def formatting_error(response) -> bool:
    description = (response or {}).get("description", "").lower()
    return (response or {}).get("error_code") == 400 and any(
        word in description for word in ("parse", "entity", "entities", "tag"))


class Client:
    def __init__(self):
        self._lock = threading.Lock()
        self._until = 0.0

    def call(self, method: str, *, timeout: float = 25, **params):
        token = config.secret("telegram.bot_token")
        if not token:
            return {"ok": False, "error_code": 401, "description": "Bot token unavailable"}
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/{method}",
            data=json.dumps(params).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        # Polling already has its own retry loop. Typing is disposable and
        # must not compete with replies during a flood-control cooldown.
        attempts = 1 if method in ("getUpdates", "sendChatAction") else 3
        deadline = time.monotonic() + 10
        response = None
        for attempt in range(attempts):
            with self._lock:
                delay = max(0, self._until - time.monotonic())
            if delay:
                if method == "sendChatAction" or time.monotonic() + delay > deadline:
                    return response or {"ok": False, "error_code": 429,
                                        "parameters": {"retry_after": delay}}
                time.sleep(delay)
                # Another topic may have extended the cooldown while asleep.
                with self._lock:
                    if self._until > time.monotonic():
                        return response or {"ok": False, "error_code": 429,
                                            "parameters": {"retry_after": self._until - time.monotonic()}}
            try:
                with urllib.request.urlopen(request, timeout=min(timeout, 10) if method == "sendChatAction" else timeout) as result:
                    response = json.load(result)
                if not isinstance(response, dict):
                    response = None
            except urllib.error.HTTPError as error:
                try:
                    response = json.loads(error.read())
                    if not isinstance(response, dict):
                        response = {}
                except (OSError, ValueError):
                    response = {}
                finally:
                    error.close()
                response = {**response, "ok": False, "error_code": error.code}
            except (urllib.error.URLError, OSError, ValueError):
                response = None
            # An already-applied edit is a successful, idempotent delivery.
            if (method == "editMessageText" and (response or {}).get("error_code") == 400
                    and "message is not modified" in (response or {}).get("description", "").lower()):
                return {"ok": True, "result": {"message_id": params.get("message_id")}}
            if response and response.get("ok"):
                return response
            code = (response or {}).get("error_code", "network/JSON")
            print(f"telegram api {method}: {code}", file=sys.stderr, flush=True)
            if not transient(response):
                return response
            delay = max(retry_after(response), min(30, 2 ** (attempt + 1)))
            with self._lock:
                self._until = max(self._until, time.monotonic() + delay)
            if attempt + 1 == attempts or time.monotonic() + delay > deadline:
                break
        return response


_client = Client()


def call(method: str, *, timeout: float = 25, **params):
    return _client.call(method, timeout=timeout, **params)
