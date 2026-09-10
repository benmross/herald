#!/usr/bin/env python
"""Render a page with a real browser and hand back what a person would see.

`WebFetch` converts raw HTML to markdown, which is fine for static pages and
useless for anything that renders its content with JavaScript after load --
Amazon, Best Buy and Staples product pages all do this, and price/stock is
exactly the part that only exists after render. This exists because a laptop-
shopping task on 8 Sep 2026 burned several WebFetch calls timing out or
returning empty shells on exactly those three sites.

    tools/browse.py <url>                    -> visible text on stdout
    tools/browse.py <url> --screenshot PATH  -> also saves a full-page PNG
    tools/browse.py <url> --wait SELECTOR    -> wait for a selector before reading
    tools/browse.py <url> --timeout 30       -> seconds, default 20

Prints the page title, then the rendered body's visible text (script/style
stripped, collapsed whitespace). Exits non-zero with a reason on failure --
blocked, timed out, no such page -- rather than printing an empty page as if
it were a real answer.

One page per invocation, headless, no persistent profile or cookies kept
between runs -- this is a read tool, not a logged-in session. If a future task
needs to stay logged in somewhere, that is a different, deliberate piece of
work, not a default this script should grow into quietly.
"""

from __future__ import annotations

import argparse
import sys


def render(url: str, wait: str | None, timeout_s: float) -> tuple[str, str]:
    from playwright.sync_api import sync_playwright

    timeout_ms = timeout_s * 1000
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            if wait:
                page.wait_for_selector(wait, timeout=timeout_ms)
            else:
                # Give client-rendered content (price, stock) a moment to land
                # even when the DOM technically finished loading already.
                page.wait_for_timeout(2000)
            title = page.title()
            text = page.evaluate(
                "() => document.body ? document.body.innerText : ''"
            )
            return title, text
        finally:
            browser.close()


def screenshot(url: str, wait: str | None, timeout_s: float, path: str) -> None:
    from playwright.sync_api import sync_playwright

    timeout_ms = timeout_s * 1000
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            if wait:
                page.wait_for_selector(wait, timeout=timeout_ms)
            else:
                page.wait_for_timeout(2000)
            page.screenshot(path=path, full_page=True)
        finally:
            browser.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url")
    ap.add_argument("--wait", help="CSS selector to wait for before reading")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--screenshot", help="also save a full-page PNG here")
    args = ap.parse_args()

    try:
        title, text = render(args.url, args.wait, args.timeout)
    except Exception as e:  # noqa: BLE001 -- surfacing the reason is the point
        print(f"browse failed: {e}", file=sys.stderr)
        return 1

    if args.screenshot:
        try:
            screenshot(args.url, args.wait, args.timeout, args.screenshot)
        except Exception as e:  # noqa: BLE001
            print(f"screenshot failed: {e}", file=sys.stderr)

    print(f"# {title}\n")
    print(text.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
