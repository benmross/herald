"""Remote browser workspaces: a laptop's Chrome driven over an SSH tunnel."""
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'lib'))
from herald import browse, browser_remote, browser_workspaces as bw, config

REMOTE = {'laptop': {'ssh': 'laptop', 'remote_port': 9333, 'local_port': 9391,
                     'profile': 'Library/Application Support/Herald Chrome'}}


class Remote(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        real_get = config.get
        self.patches = [mock.patch.object(config, 'HOME', root),
                        mock.patch.object(config, 'link_ledger', return_value=None),
                        mock.patch.object(config, 'LEDGER', root / 'ledger'),
                        mock.patch.object(config, 'DB_PATH', root / 'facts.db'),
                        mock.patch.object(config, 'get', side_effect=lambda k, d=None: REMOTE if k == 'browser.remote' else real_get(k, d)),
                        mock.patch.object(bw, 'available_memory_mb', return_value=12000)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_remote_workspace_uses_tunnel_port_and_negative_slot(self):
        self.assertTrue(browser_remote.is_remote('laptop'))
        self.assertFalse(browser_remote.is_remote('default'))
        self.assertEqual(browse.cdp_port('laptop'), 9391)
        row = bw.get('laptop')
        self.assertLess(row['slot'], 0)
        self.assertEqual(row['state'], 'agent')

    def test_remote_is_never_auto_chosen_or_grouped(self):
        bw.get('laptop')
        with mock.patch.object(bw, 'managed_workers', return_value=False):
            self.assertEqual(bw.choose(1, 'https://example.com/job'), 'default')
        self.assertIsNone(bw.ready_group('laptop'))
        bw.start('laptop')  # no systemd worker for a remote device

    def test_launch_is_background_and_separate_profile(self):
        cmd = browser_remote.launch_command(REMOTE['laptop'])
        self.assertIn('open -g -n', cmd)
        self.assertIn('--remote-debugging-address=127.0.0.1', cmd)
        self.assertIn('Herald Chrome', cmd)


if __name__ == '__main__':
    unittest.main()
