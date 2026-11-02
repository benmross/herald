"""Private, lossless event envelopes for debugging future engine launches.

Records provider-visible data, never hidden model state. Private mode bypasses
this module entirely. Credentials are redacted before bytes reach disk.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import threading
import time
import uuid

from . import config, private


class Trace:
    def __init__(self, **metadata):
        self.file = None
        self.lock = threading.Lock()
        self.start = time.monotonic_ns()
        self.seq = 0
        if private.active() or not config.get("debug.capture", True):
            return
        try:
            from .debugui import redactor
            self.clean = redactor()
            # Snapshot known project instructions without walking arbitrary
            # directories or provider state (which can contain credentials).
            instructions = []
            for name in ("CLAUDE.md", "AGENTS.md"):
                path = config.ROOT / name
                if path.is_file():
                    content = path.read_text(errors="replace")
                    instructions.append({"name": name, "sha256": hashlib.sha256(content.encode()).hexdigest(), "text": content})
            metadata["project_instructions_at_launch"] = instructions
            directory = config.HOME / "debug-traces"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = directory / (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S-") + uuid.uuid4().hex + ".jsonl")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self.file = os.fdopen(fd, "w", buffering=1)
            self.emit("launch", metadata)
        except (OSError, ValueError):
            self.file = None

    def emit(self, channel, data):
        if self.file is None:
            return
        try:
            if channel == "stdout" and isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    pass
            with self.lock:
                self.seq += 1
                row = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds"),
                       "elapsed_ns": time.monotonic_ns() - self.start,
                       "seq": self.seq, "channel": channel, "data": data}
                # Redact values recursively so JSON escapes cannot hide a token.
                self.file.write(json.dumps(self.clean(row), ensure_ascii=False) + "\n")
        except (OSError, ValueError):
            pass  # Observability must never interrupt a conversation.

    def close(self, **result):
        self.emit("finish", result)
        if self.file is not None:
            try:
                self.file.close()
            except OSError:
                pass
