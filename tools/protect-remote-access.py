#!/usr/bin/env python3
"""Review/apply host memory priorities without restarting remote access.

Run --uid UID to print the complete plan. --apply requires root. The Herald
root approval door must approve this exact operation; this tool grants no
privilege by itself. Existing drop-ins are backed up before replacement.
"""
from __future__ import annotations
import argparse
import datetime
import json
import os
import pathlib
import pwd
import shutil
import subprocess
import tempfile


def plan(uid: int) -> dict[str, str]:
    if uid <= 0:
        raise ValueError('Choose the non-root Herald account')
    return {
        'system.slice': '[Slice]\nMemoryMin=96M\nMemoryLow=256M\n',
        'ssh.service': '[Service]\nOOMScoreAdjust=-1000\nMemoryMin=64M\nMemoryLow=128M\nManagedOOMPreference=omit\n',
        'tailscaled.service': '[Service]\nOOMScoreAdjust=-900\nMemoryMin=32M\nMemoryLow=128M\nManagedOOMPreference=omit\n',
        'user.slice': '[Slice]\nMemoryLow=256M\n',
        f'user-{uid}.slice': '[Slice]\nMemoryLow=256M\n',
        f'user@{uid}.service': '[Service]\nOOMScoreAdjust=-500\nMemoryLow=256M\n',
    }


def apply(uid: int, changes: dict[str, str]) -> None:
    if os.geteuid() != 0:
        raise PermissionError('--apply requires the separately approved root command')
    pwd.getpwuid(uid)  # fail before touching anything for a nonexistent account
    # Resolve the OpenSSH unit alias on distributions that call it sshd.
    candidates = ('ssh.service', 'sshd.service')
    ssh = next((u for u in candidates if subprocess.run(
        ['systemctl', 'show', u, '-p', 'LoadState', '--value'],
        capture_output=True, text=True, check=True).stdout.strip() == 'loaded'), None)
    if not ssh:
        raise RuntimeError('No loaded SSH service; no changes made')
    if subprocess.run(['systemctl', 'show', 'tailscaled.service', '-p', 'LoadState', '--value'],
                      capture_output=True, text=True, check=True).stdout.strip() != 'loaded':
        raise RuntimeError('No loaded Tailscale service; no changes made')
    changes = {(ssh if unit == 'ssh.service' else unit): content for unit, content in changes.items()}
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = pathlib.Path('/var/lib/herald-resilience/backups') / stamp
    backup.mkdir(parents=True, mode=0o700)
    for unit, content in changes.items():
        directory = pathlib.Path('/etc/systemd/system') / (unit + '.d')
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'zz-herald-memory-protection.conf'
        if path.exists():
            shutil.copy2(path, backup / (unit + '.conf'))
        else:
            (backup / (unit + '.absent')).touch()
        with tempfile.NamedTemporaryFile('w', dir=directory, delete=False) as f:
            f.write('# Managed by protect-remote-access.py; backups in /var/lib/herald-resilience\n' + content)
            f.flush(); os.fsync(f.fileno())
            temporary = pathlib.Path(f.name)
        temporary.chmod(0o644)
        temporary.replace(path)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    # Resource controls apply live. OOMScoreAdjust applies on exec, so update
    # ONLY the live service processes as well, with the same persistent values.
    for unit, content in changes.items():
        props = [line for line in content.splitlines() if line.startswith(('MemoryMin=', 'MemoryLow=', 'ManagedOOMPreference='))]
        if props:
            subprocess.run(['systemctl', 'set-property', '--runtime', unit, *props], check=True)
        score = next((line.split('=', 1)[1] for line in content.splitlines() if line.startswith('OOMScoreAdjust=')), None)
        if score is not None:
            pid = int(subprocess.run(['systemctl', 'show', unit, '-p', 'MainPID', '--value'],
                      capture_output=True, text=True, check=True).stdout.strip() or 0)
            if pid:
                (pathlib.Path('/proc') / str(pid) / 'oom_score_adj').write_text(score)
    # Existing SSH connection handlers can have a different score from sshd.
    # Protect the transport processes, not shells or arbitrary login workloads.
    for proc in pathlib.Path('/proc').iterdir():
        if not proc.name.isdigit(): continue
        try:
            if (proc / 'comm').read_text().strip() in ('sshd', 'sshd-session', 'sshd-auth'):
                (proc / 'oom_score_adj').write_text('-1000')
        except FileNotFoundError:
            pass
    print('Applied without restarting SSH, Tailscale, or the user manager.')
    print('Backup:', backup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uid', type=int, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    changes = plan(args.uid)
    print(json.dumps(changes, indent=2))
    if args.apply:
        apply(args.uid, changes)


if __name__ == '__main__': main()
