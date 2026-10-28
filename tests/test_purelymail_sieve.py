"""ManageSieve protocol parsing and the approval boundary for delivery changes."""
import io
import pathlib
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'lib'))
from herald import purelymail, purelymail_sieve as sieve


class Sieve(unittest.TestCase):
    def client(self, response):
        sock=Mock()
        sock.makefile.return_value=io.BytesIO(response)
        return sieve.Sieve(sock)

    def test_read_literal_exactly(self):
        client=self.client(b'{7}\r\nkeep;\r\n\r\nOK\r\n')
        self.assertEqual(client.get('forward'), 'keep;\r\n')
        client.sock.sendall.assert_called_once_with(b'GETSCRIPT "forward"\r\n')

    def test_scripts_active_and_escaped_names(self):
        client=self.client(b'"a\\"b"\r\n"forward" ACTIVE\r\nOK\r\n')
        self.assertEqual(client.scripts(), [('a"b',False),('forward',True)])

    def test_unapproved_writes_never_reach_socket(self):
        client=self.client(b'OK\r\n')
        with self.assertRaises(PermissionError):
            client.put('forward','keep;')
        client.sock.sendall.assert_not_called()
        with self.assertRaises(PermissionError):
            client.activate('forward')
        client.sock.sendall.assert_not_called()

    def test_approved_putscript_counts_utf8_bytes(self):
        client=self.client(b'OK\r\n')
        with patch.object(sieve.red,'acting',return_value=1):
            client.put('forward','# café\nkeep;\n')
        script='# café\nkeep;\n'.encode()
        self.assertEqual(client.sock.sendall.call_args.args[0],
                         b'PUTSCRIPT "forward" {'+str(len(script)).encode()+b'+}\r\n'+script+b'\r\n')

    def test_string_rejects_command_injection(self):
        with self.assertRaises(ValueError):
            sieve.quote('x\r\nSETACTIVE "bad"')

    def test_route_refuses_without_tap(self):
        with patch.object(purelymail.urllib.request,'urlopen') as request:
            with self.assertRaises(PermissionError):
                purelymail.create_local_route(user='me',domain='example.org',address='me@example.org')
            request.assert_not_called()


if __name__=='__main__':
    unittest.main()
