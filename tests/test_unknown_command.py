"""A mistyped command is refused by the bridge and never reaches a session."""
import importlib.machinery
import importlib.util
import pathlib
import sys
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))

loader = importlib.machinery.SourceFileLoader('tg_unknown_test', str(ROOT / 'bin/herald-telegram'))
spec = importlib.util.spec_from_loader('tg_unknown_test', loader)
tg = importlib.util.module_from_spec(spec)
loader.exec_module(tg)


class UnknownCommand(unittest.TestCase):
    def test_only_unknown_bare_commands_are_refused(self):
        for text in ('/new', '/NEW', '/new@some_bot', '/effort high', '/compact',
                     '/home/someone/file.txt', 'hello /nwe', ''):
            with self.subTest(text=text):
                self.assertEqual(tg.unknown_command(text), '')
        self.assertEqual(tg.unknown_command('/nwe'), '/nwe')
        self.assertEqual(tg.unknown_command('/nwe and more'), '/nwe')

    def test_dispatch_answers_without_a_turn(self):
        update = {'update_id': 1, 'message': {'text': '/nwe', 'chat': {'id': 5},
                                              'from': {'id': 7}, 'message_thread_id': 3}}
        with patch.object(tg.config, 'secret', return_value=7), \
             patch.object(tg, 'send') as send, patch.object(tg, 'handle') as handle, \
             patch.object(tg, '_checkpoint'), patch.object(tg, '_maybe_restart'):
            tg._dispatch(update, {})
        handle.assert_not_called()
        self.assertIn('Did you mean /new?', send.call_args[0][1])


if __name__ == '__main__':
    unittest.main()
