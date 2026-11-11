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


class StopCommand(unittest.TestCase):
    def test_stop_uses_the_same_parser_as_other_commands(self):
        for text in ('/stop', '/STOP', ' /stop@some_bot ', '/stop@some_bot now'):
            with self.subTest(text=text):
                self.assertTrue(tg._is_stop_command({'message': {'text': text}}))
                self.assertTrue(tg._is_stop_command({'edited_message': {'text': text}}))
        for text in ('please /stop', '/stopping', '/stop/path', ''):
            self.assertFalse(tg._is_stop_command({'message': {'text': text}}))

    def test_addressed_stop_cancels_pending_resume_without_a_model(self):
        ts = {'session_id': 'limited', 'last_ts': 10}
        tg.limitresume.schedule(ts, 2000, 'claude', 5, 3)
        state = {'threads': {'5:3': ts}}
        update = {'update_id': 1, 'message': {'text': '/stop@some_bot',
                  'chat': {'id': 5}, 'from': {'id': 7}, 'message_thread_id': 3}}
        with patch.object(tg.config, 'secret', return_value=7), \
             patch.object(tg.think, 'cancel', return_value=False) as cancel, \
             patch.object(tg, 'send') as send, patch.object(tg, 'handle') as handle, \
             patch.object(tg, '_checkpoint'), patch.object(tg, '_maybe_restart'):
            tg._dispatch(update, state)
        cancel.assert_called_once_with('5:3')
        handle.assert_not_called()
        self.assertEqual(tg.limitresume.take_due(state['threads'], 3000), [])
        self.assertGreater(ts['last_ts'], 10)
        self.assertIn("won't continue", send.call_args[0][1])

    def test_addressed_stop_interrupts_an_active_turn(self):
        update = {'update_id': 1, 'message': {'text': '/stop@some_bot',
                  'chat': {'id': 5}, 'from': {'id': 7}, 'message_thread_id': 3}}
        with patch.object(tg.config, 'secret', return_value=7), \
             patch.object(tg.think, 'cancel', return_value=True) as cancel, \
             patch.object(tg, 'send') as send, patch.object(tg, 'handle') as handle, \
             patch.object(tg, '_checkpoint'), patch.object(tg, '_maybe_restart'):
            tg._dispatch(update, {})
        cancel.assert_called_once_with('5:3')
        handle.assert_not_called()
        self.assertIn('Stopped', send.call_args[0][1])

    def test_someone_else_cannot_cancel_a_continuation(self):
        ts = {}
        tg.limitresume.schedule(ts, 2000, 'claude', 5, 3)
        state = {'threads': {'5:3': ts}}
        update = {'update_id': 1, 'message': {'text': '/stop@some_bot',
                  'chat': {'id': 5}, 'from': {'id': 8}, 'message_thread_id': 3}}
        with patch.object(tg.config, 'secret', return_value=7), \
             patch.object(tg.think, 'cancel') as cancel, patch.object(tg, 'send') as send, \
             patch.object(tg, '_checkpoint'), patch.object(tg, '_maybe_restart'):
            tg._dispatch(update, state)
        cancel.assert_not_called()
        send.assert_not_called()
        self.assertIn(tg.limitresume.KEY, ts)


if __name__ == '__main__':
    unittest.main()
