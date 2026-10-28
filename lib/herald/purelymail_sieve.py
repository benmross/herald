"""Small TLS-only ManageSieve client for Purelymail (RFC 5804).

Read operations need only mailbox authentication. Writes additionally require
red.acting(), so scripts cannot be uploaded or activated outside a tapped action.
"""
import base64
import re
import socket
import ssl
from contextlib import contextmanager

from . import config, red


class Sieve:
    def __init__(self, sock):
        self.sock = sock
        self.stream = sock.makefile('rb')

    def response(self):
        rows = []
        while True:
            line = self.stream.readline(65537)
            if not line or len(line) > 65536:
                raise RuntimeError('Invalid ManageSieve response')
            if line.startswith(b'OK'):
                return rows
            if line.startswith((b'NO', b'BYE')):
                raise RuntimeError('Purelymail rejected the ManageSieve operation')
            literal = re.fullmatch(rb'\{(\d+)\+?\}\r\n', line)
            if literal:
                length = int(literal[1])
                if length > 1_000_000:
                    raise RuntimeError('Sieve script exceeds read limit')
                data = self.stream.read(length)
                if len(data) != length or self.stream.read(2) != b'\r\n':
                    raise RuntimeError('Incomplete Sieve script')
                rows.append(data)
            else:
                rows.append(line.rstrip(b'\r\n'))

    def command(self, command):
        if command.split(b' ', 1)[0] not in (b'CAPABILITY', b'STARTTLS', b'AUTHENTICATE',
                                            b'LISTSCRIPTS', b'GETSCRIPT', b'LOGOUT', b'CHECKSCRIPT'):
            if red.acting() is None:
                raise PermissionError('Sieve writes require a tapped red action')
        self.sock.sendall(command + b'\r\n')
        return self.response()

    def scripts(self):
        scripts = []
        for line in self.command(b'LISTSCRIPTS'):
            m = re.fullmatch(rb'"((?:[^"\\]|\\.)*)"(?: (ACTIVE))?', line)
            if not m:
                raise RuntimeError('Unsupported Sieve script name response')
            name = re.sub(rb'\\(.)', rb'\1', m[1]).decode('utf-8')
            scripts.append((name, bool(m[2])))
        return scripts

    def get(self, name):
        rows = self.command(b'GETSCRIPT ' + quote(name))
        if len(rows) != 1:
            raise RuntimeError('Unexpected Sieve script response')
        return rows[0].decode('utf-8')

    def put(self, name, script):
        raw = script.encode('utf-8')
        self.command(b'PUTSCRIPT ' + quote(name) + b' {' + str(len(raw)).encode() + b'+}\r\n' + raw)

    def activate(self, name):
        self.command(b'SETACTIVE ' + quote(name))


def quote(text):
    if '\r' in text or '\n' in text or '\0' in text:
        raise ValueError('Invalid Sieve string')
    return ('"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"').encode('utf-8')


@contextmanager
def connect():
    sock = socket.create_connection(('mailserver.purelymail.com', 4190), timeout=30)
    client = Sieve(sock)
    try:
        client.response()
        client.command(b'STARTTLS')
        client.stream.close()
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname='mailserver.purelymail.com')
        client = Sieve(sock)
        client.command(b'CAPABILITY')
        auth = '\0' + config.get('purelymail.address') + '\0' + config.secret('purelymail.password')
        client.command(b'AUTHENTICATE "PLAIN" "' + base64.b64encode(auth.encode()) + b'"')
        yield client
    finally:
        client.stream.close()
        sock.close()
