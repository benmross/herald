"""Browser workspaces that live on another machine, reached over SSH.

A remote workspace is a dedicated Chrome profile on a device the person uses
(their laptop), started in the background with remote debugging bound to that
device's loopback, and forwarded here with `ssh -L`. The ordinary apply code
drives it through the forwarded CDP port, so a form is filled in the browser
where the person reviews and submits it, with no remote-screen hop. Playwright
streams file uploads over CDP, so the resume does not need copying first
(verified 15 Nov 2026 with Chrome 157 on macOS and Playwright 1.62).

Configured under `browser.remote`, keyed by workspace name:

    {"macbook": {"ssh": "macbook", "remote_port": 9333, "local_port": 9390,
                 "profile": "Library/Application Support/Herald Chrome"}}

`profile` is relative to the remote home. Chrome refuses remote debugging on
its default profile, and a separate profile keeps automation away from the
person's own tabs and sign-ins. `open -g` starts it without taking focus.
Cookies never cross machines: the server profile's auth file is neither
copied in nor overwritten from here.
"""
from __future__ import annotations

import shlex
import socket
import subprocess
import time
import urllib.request

from . import config


def all_remote() -> dict:
    return dict(config.get('browser.remote', {}) or {})


def spec(workspace) -> dict | None:
    s = all_remote().get(workspace)
    return dict(s) if isinstance(s, dict) and s.get('ssh') else None


def is_remote(workspace) -> bool:
    return spec(workspace) is not None


def local_port(workspace) -> int:
    return int(spec(workspace).get('local_port', 9390))


def _alive(port, timeout=2) -> bool:
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _ssh(host, command, timeout=25):
    return subprocess.run(['ssh', '-o', 'ConnectTimeout=8', '-o', 'BatchMode=yes', host, command],
                          capture_output=True, text=True, timeout=timeout)


def _tunnel(s, port):
    with socket.socket() as sock:
        if sock.connect_ex(('127.0.0.1', port)) == 0:
            return  # a forward already listens; _alive decides whether it works
    subprocess.run(['ssh', '-f', '-N', '-o', 'ExitOnForwardFailure=yes', '-o', 'BatchMode=yes',
                    '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
                    '-L', f'127.0.0.1:{port}:127.0.0.1:{int(s.get("remote_port", 9333))}', s['ssh']],
                   capture_output=True, text=True, timeout=20, check=False)


def launch_command(s) -> str:
    profile = s.get('profile', 'Library/Application Support/Herald Chrome')
    app = s.get('app', 'Google Chrome')
    rport = int(s.get('remote_port', 9333))
    return (f'curl -sf --max-time 2 http://127.0.0.1:{rport}/json/version >/dev/null || '
            f'open -g -n -a {shlex.quote(app)} --args --user-data-dir="$HOME/"{shlex.quote(profile)} '
            f'--remote-debugging-port={rport} --remote-debugging-address=127.0.0.1 '
            '--no-first-run --no-default-browser-check')


def ensure(workspace) -> None:
    s = spec(workspace)
    port = local_port(workspace)
    if _alive(port):
        return
    r = _ssh(s['ssh'], launch_command(s))
    if r.returncode not in (0,):
        raise RuntimeError(f'{workspace} is unreachable over SSH (asleep or offline); nothing was filled.')
    for _ in range(3):
        _tunnel(s, port)
        for _ in range(20):
            if _alive(port):
                minimize(port)
                return
            time.sleep(0.5)
        # A stale forward whose SSH session died holds the port; clear it.
        subprocess.run(['pkill', '-f', '--', f'-L 127.0.0.1:{port}:'], check=False)
    raise RuntimeError(f'The browser on {workspace} did not answer through the SSH tunnel.')


def minimize(port) -> None:
    """Minimize the remote browser's windows. `open -g` keeps focus where it
    was but still shows a window; hiding the app instead made the next tab
    unhide and activate it (15 Nov 2026). A minimized window stays down while
    background tabs open and fill."""
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')
            cdp = b.new_browser_cdp_session()
            seen = set()
            for t in cdp.send('Target.getTargets')['targetInfos']:
                if t['type'] != 'page':
                    continue
                w = cdp.send('Browser.getWindowForTarget', {'targetId': t['targetId']})['windowId']
                if w not in seen:
                    seen.add(w)
                    cdp.send('Browser.setWindowBounds', {'windowId': w, 'bounds': {'windowState': 'minimized'}})
            b.close()
    except Exception:
        pass  # a visible window is a nuisance, not a failure


def quiet(browser, ctx) -> None:
    """Open tabs in the background and never raise one, so filling on the
    person's laptop does not take focus from what they are doing. Playwright's
    new_page() activates the window; Target.createTarget with background does
    not."""
    cdp = browser.new_browser_cdp_session()

    def new_page():
        with ctx.expect_page() as info:
            cdp.send('Target.createTarget', {'url': 'about:blank', 'background': True})
        page = info.value
        _hush(page)
        return page

    def _hush(page):
        try:
            page.bring_to_front = lambda: None
        except Exception:
            pass

    ctx.new_page = new_page
    for page in ctx.pages:
        _hush(page)
    ctx.on('page', _hush)
