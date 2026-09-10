#!/usr/bin/env python
"""Deep-pull mail with bodies, for one-off analysis.

The gmail collector deliberately stores metadata and snippets only: it runs
every 30 minutes and full bodies would be a lot of disk for very little the
snippet does not carry. But some jobs genuinely need the prose -- building a
voice profile out of how the user actually writes, or sweeping years of mail for
things the user still owes someone.

This is that escape hatch. It writes JSONL to ledger/raw/ and touches nothing
else, so it is safe to re-run and cheap to throw away.

    tools/pull-mail.py --query "in:sent" --out voice/sent.jsonl
    tools/pull-mail.py --query "-in:sent newer_than:400d" --out sweep/received.jsonl
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))

from herald import config, google  # noqa: E402

BATCH = 25
PAUSE = 0.4
BODY_CAP = 20_000

_QUOTED = re.compile(
    r"^\s*(>|On .{5,80} wrote:|-{2,} ?Forwarded message|_{5,}|From: )", re.M)


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode()).decode("utf-8", "replace")


def _plain_text(payload: dict) -> str:
    """Best available plain text, preferring text/plain over stripped HTML."""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})

    if mime == "text/plain" and body.get("data"):
        return _decode(body["data"])
    if mime == "text/html" and body.get("data"):
        html = _decode(body["data"])
        html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        html = re.sub(r"(?i)<br\s*/?>|</p>", "\n", html)
        return re.sub(r"<[^>]+>", " ", html)

    best = ""
    for part in payload.get("parts", []) or []:
        text = _plain_text(part)
        # Prefer the plain alternative when a message carries both.
        if part.get("mimeType") == "text/plain" and text.strip():
            return text
        if len(text) > len(best):
            best = text
    return best


def _own_words(text: str) -> str:
    """Drop quoted history so a voice profile sees what the user wrote, not replies."""
    cut = _QUOTED.search(text)
    if cut and cut.start() > 40:
        text = text[:cut.start()]
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _header(msg: dict, name: str) -> str | None:
    want = name.lower()
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == want:
            return h.get("value")
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True, help="Gmail search query")
    ap.add_argument("--out", required=True, help="path under ledger/raw/")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--min-chars", type=int, default=0,
                    help="skip messages whose own-words body is shorter")
    args = ap.parse_args()

    svc = google.service("gmail", "v1")

    ids, page = [], None
    while len(ids) < args.limit:
        resp = svc.users().messages().list(
            userId="me", q=args.query, maxResults=500, pageToken=page).execute()
        ids.extend(m["id"] for m in resp.get("messages", []))
        page = resp.get("nextPageToken")
        if not page:
            break
    ids = ids[:args.limit]
    print(f"{len(ids)} messages match {args.query!r}")

    out = config.RAW / args.out
    out.parent.mkdir(parents=True, exist_ok=True)

    fetched, failed = {}, []

    def _capture(request_id, response, exception):
        if exception is not None:
            failed.append(request_id)
        elif response:
            fetched[response["id"]] = response

    for i in range(0, len(ids), BATCH):
        batch = svc.new_batch_http_request(callback=_capture)
        for mid in ids[i:i + BATCH]:
            batch.add(svc.users().messages().get(userId="me", id=mid, format="full"),
                      request_id=mid)
        batch.execute()
        time.sleep(PAUSE)
        print(f"  {min(i + BATCH, len(ids))}/{len(ids)}", end="\r", flush=True)

    for mid in list(failed):
        try:
            fetched[mid] = svc.users().messages().get(
                userId="me", id=mid, format="full").execute()
            failed.remove(mid)
        except Exception:                                 # noqa: BLE001
            pass
        time.sleep(PAUSE)

    written = 0
    with out.open("w", encoding="utf-8") as fh:
        for mid, msg in sorted(fetched.items(),
                               key=lambda kv: int(kv[1].get("internalDate", 0))):
            text = _own_words(_plain_text(msg.get("payload", {})))[:BODY_CAP]
            if len(text) < args.min_chars:
                continue
            fh.write(json.dumps({
                "id": mid,
                "thread_id": msg.get("threadId"),
                "date": _header(msg, "Date"),
                "from": _header(msg, "From"),
                "to": _header(msg, "To"),
                "cc": _header(msg, "Cc"),
                "subject": _header(msg, "Subject"),
                "labels": msg.get("labelIds", []),
                "snippet": msg.get("snippet"),
                "text": text,
            }, ensure_ascii=False) + "\n")
            written += 1

    size = out.stat().st_size / 1e6
    print(f"\nwrote {written} messages to {out} ({size:.1f} MB)"
          + (f"; {len(failed)} unrecoverable" if failed else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
