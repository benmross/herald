"""Direct Purelymail mailbox access. IMAP reads and logged drafts; no sending.

The administration API cannot read messages. Use an app password over TLS,
readonly SELECT and BODY.PEEK so collecting never marks mail read. Each folder
has a UIDVALIDITY/UID checkpoint; first sync covers the configured recent window.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import imaplib
import json
import os
import pathlib
import re
import ssl
import urllib.request
from contextlib import contextmanager
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, getaddresses, make_msgid, parsedate_to_datetime

from . import collector, config, db, mailtext


def check(status, operation):
    if status != 'OK':
        raise RuntimeError(f'Purelymail {operation} failed')


@contextmanager
def mailbox():
    address = config.get('purelymail.address')
    password = config.secret('purelymail.password')
    if not address or not password:
        raise RuntimeError('Set purelymail.address and request purelymail.password (an app password).')
    client = imaplib.IMAP4_SSL('imap.purelymail.com', 993,
                              ssl_context=ssl.create_default_context(), timeout=45)
    try:
        # Never include the server response in a login error: credentials stay private.
        try:
            check(client.login(address, password)[0], 'login')
        except imaplib.IMAP4.error:
            raise RuntimeError('Purelymail login failed; check the app password.') from None
        yield client
    finally:
        try:
            client.logout()
        except (OSError, imaplib.IMAP4.error):
            pass


def folders(client):
    status, rows = client.list()
    check(status, 'folder listing')
    result = []
    for row in rows or []:
        if not isinstance(row, bytes):
            raise RuntimeError('Unsupported IMAP literal folder name')
        match = re.fullmatch(rb'\((.*?)\) (?:"(?:[^"\\]|\\.)*"|NIL) (.+)', row)
        if not match:
            raise RuntimeError('Unrecognized IMAP folder listing')
        flags = match[1].lower().split()
        if b'\\noselect' not in flags:
            # Keep the IMAP quoting and modified UTF-7 name intact.
            result.append((match[2].decode('ascii'), flags))
    return result


def mime_payload(msg):
    """Adapt MIME to the shared text/image reader's payload shape."""
    if msg.is_multipart():
        return {'mimeType': msg.get_content_type(),
                'parts': [mime_payload(part) for part in msg.iter_parts()]}
    raw = msg.get_payload(decode=True) or b''
    if msg.get_content_maintype() == 'text':
        text = raw.decode(msg.get_content_charset() or 'utf-8', errors='replace')
        raw = text.encode('utf-8')
    return {'mimeType': msg.get_content_type(), 'filename': msg.get_filename() or '',
            'body': {'data': base64.urlsafe_b64encode(raw).decode(), 'size': len(raw)}}


def images(message_id, destination, seen, settings, remaining):
    from . import mailimages
    path = config.RAW / 'purelymail' / f'{message_id}.eml'
    if not path.exists():
        return [], {'failed': 1}
    if not (settings['attachments'] or settings['remote']) or remaining <= 0:
        return [], {}
    msg = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    found = mailtext.images(mime_payload(msg))
    if not settings['attachments']:
        found = [f for f in found if f['kind'] != 'part']
    return mailimages.collect(found, destination, seen=seen, remote=settings['remote'],
                              limit=min(settings['per_message'], remaining))


def parse_message(raw, *, folder, uid, validity):
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    body, _ = mailtext.body(mime_payload(msg))
    address = config.get('purelymail.address', '').lower()
    from_me = address in {a.lower() for _, a in getaddresses([str(msg.get('From', ''))])}
    try:
        date = parsedate_to_datetime(str(msg.get('Date', '')))
        if date.tzinfo is None:
            date = date.replace(tzinfo=dt.timezone.utc)
        timestamp = date.astimezone(dt.timezone.utc).isoformat(timespec='seconds')
    except (ValueError, TypeError, OverflowError):
        timestamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    message_id = str(msg.get('Message-ID', '')).strip()
    identity = message_id or f'{folder}:{validity}:{uid}'
    external_id = hashlib.sha256(f'{address}:{identity}'.encode()).hexdigest()
    attachments = [{'filename': p.get_filename(), 'mime_type': p.get_content_type(),
                    'size': len(p.get_payload(decode=True) or b'')}
                   for p in msg.walk() if p.get_filename()]
    bulk = bool(msg.get('List-Id') or msg.get('List-Unsubscribe')
                or str(msg.get('Precedence', '')).lower() in ('bulk', 'list', 'junk')
                or str(msg.get('Auto-Submitted', 'no')).lower() != 'no'
                or re.search(r'no[-_.]?reply|mailer-daemon', str(msg.get('From', '')), re.I))
    return external_id, timestamp, str(msg.get('Subject', '')), body[:mailtext.MAX_CHARS], {
        'account': address, 'folder': folder, 'uid': uid, 'uidvalidity': validity,
        'message_id': message_id, 'from': str(msg.get('From', '')),
        'to': str(msg.get('To', '')), 'cc': str(msg.get('Cc', '')),
        'reply_to': str(msg.get('Reply-To', '')), 'from_me': from_me, 'bulk': bulk,
        'list_id': str(msg.get('List-Id', '')), 'in_reply_to': str(msg.get('In-Reply-To', '')),
        'references': str(msg.get('References', '')), 'attachments': attachments,
        'snippet': mailtext.compact(body)[:300], 'body_truncated': len(body) > mailtext.MAX_CHARS,
    }


def collect(con):
    previous = json.loads(collector.cursor(con, 'purelymail') or '{}')
    address = config.get('purelymail.address')
    state = previous.get('folders', {}) if previous.get('account') == address else {}
    result = {}
    count = 0
    days = int(config.get('purelymail.initial_days', 21))
    limit = int(config.get('purelymail.batch_per_folder', 100))
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime('%d-%b-%Y')
    with mailbox() as client:
        for folder, flags in folders(client):
            if b'\\drafts' in flags or folder.strip('"').lower() in ('drafts', 'trash', 'junk', 'spam'):
                continue
            check(client.select(folder, readonly=True)[0], 'select')
            response = client.response('UIDVALIDITY')[1]
            if not response or not response[0]:
                raise RuntimeError('Purelymail did not supply UIDVALIDITY')
            validity = response[0].decode('ascii')
            old = state.get(folder, {})
            last = int(old.get('uid', 0)) if old.get('validity') == validity else 0
            criteria = ('UID', f'{last + 1}:*') if last else ('SINCE', since)
            status, rows = client.uid('SEARCH', None, *criteria)
            check(status, 'search')
            uids = sorted(int(u) for u in (rows[0] or b'').split() if int(u) > last)
            for uid in uids[:limit]:
                status, fetched = client.uid('FETCH', str(uid), '(BODY.PEEK[])')
                check(status, 'fetch')
                raw = next((x[1] for x in fetched if isinstance(x, tuple)), None)
                if raw is None:
                    # Do not advance past an unfetched message, even on expunge races.
                    raise RuntimeError('Purelymail message disappeared during fetch; retry sync')
                eid, timestamp, subject, body, data = parse_message(raw, folder=folder, uid=uid, validity=validity)
                db.put_fact(con, 'purelymail', 'message', external_id=eid,
                            ts=timestamp, title=subject, body=body, data=data)
                cache = config.LEDGER / 'raw' / 'purelymail'
                cache.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = cache / f'{eid}.eml'
                path.write_bytes(raw)
                path.chmod(0o600)
                last = uid
                count += 1
            result[folder] = {'validity': validity, 'uid': last}
    return {'messages': count, '_cursor': json.dumps({'account': address, 'folders': result})}


def draft(con, *, to, subject, body, cc=None, in_reply_to=None, references=None, attachments=()):
    if os.environ.get('HERALD_PRIVATE'):
        raise PermissionError('Draft creation requires leaving private mode')
    msg = EmailMessage()
    msg['From'] = config.get('purelymail.address')
    msg['To'], msg['Subject'] = to, subject
    msg['Date'], msg['Message-ID'] = formatdate(localtime=True), make_msgid()
    if cc:
        msg['Cc'] = cc
    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
    if references:
        msg['References'] = references
    msg.set_content(body)
    for filename in attachments:
        path = pathlib.Path(filename)
        msg.add_attachment(path.read_bytes(), maintype='application', subtype='octet-stream', filename=path.name)
    with mailbox() as client:
        listed = folders(client)
        folder = next((name for name, flags in listed if b'\\drafts' in flags), None)
        if folder is None:
            configured = config.get('purelymail.drafts_folder', 'Drafts')
            folder = next((name for name, _ in listed if name.strip('"') == configured), None)
        if folder is None:
            raise RuntimeError('No Drafts folder found; set purelymail.drafts_folder to an existing folder')
        status, _ = client.append(folder, r'(\Draft)', None, msg.as_bytes(policy=policy.SMTP))
        check(status, 'draft append')
    db.record_action(con, actor='session', tier='green', kind='purelymail.draft',
                     target=to, summary=f"drafted '{subject[:60]}' to {to}", ref=msg['Message-ID'])
    con.commit()
    return {'message_id': msg['Message-ID'], 'folder': folder}


def routing_rules():
    """Read administration routing, without exposing the API credential."""
    key = config.secret('purelymail.api_key')
    if not key:
        raise RuntimeError('Request purelymail.api_key to inspect routing')
    request = urllib.request.Request('https://purelymail.com/api/v0/listRoutingRules',
                                    data=b'{}', headers={'Content-Type': 'application/json',
                                    'Purelymail-Api-Token': key})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if result.get('type') != 'success':
        raise RuntimeError('Purelymail routing query failed')
    return result['result']['rules']


def create_local_route(*, user, domain, address):
    """Only the tapped extension performer may add a local-delivery rule."""
    from . import red
    if red.acting() is None:
        raise PermissionError('Routing changes require a tapped approval')
    request = urllib.request.Request('https://purelymail.com/api/v0/createRoutingRule',
        data=json.dumps({'domainName': domain, 'prefix': False, 'matchUser': user,
                         'targetAddresses': [address], 'catchall': False}).encode(),
        headers={'Content-Type': 'application/json',
                 'Purelymail-Api-Token': config.secret('purelymail.api_key')})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if result.get('type') != 'success':
        raise RuntimeError('Purelymail local routing creation failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    commands.add_parser('routing')
    p = commands.add_parser('draft')
    p.add_argument('--to', required=True)
    p.add_argument('--subject', required=True)
    p.add_argument('--body-file', required=True)
    p.add_argument('--cc')
    p.add_argument('--in-reply-to')
    p.add_argument('--references')
    p.add_argument('--attach', action='append', default=[])
    args = parser.parse_args()
    if args.command == 'routing':
        print(json.dumps(routing_rules()))
    elif args.command == 'status':
        with mailbox() as client:
            print(json.dumps({'account': config.get('purelymail.address'), 'folders': [f for f, _ in folders(client)]}))
    else:
        with db.session() as con:
            print(json.dumps(draft(con, to=args.to, subject=args.subject,
                                   body=pathlib.Path(args.body_file).read_text(), cc=args.cc,
                                   in_reply_to=args.in_reply_to, references=args.references,
                                   attachments=args.attach)))
    return 0
