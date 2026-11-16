"""Pairing the bot in setup pins the command guide in the chat."""
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT))
from setup import telegram
from setup.engine import State


class Pairing(unittest.TestCase):
    def setUp(self):
        self.secrets = {'telegram.bot_token': '1:fake'}
        self.calls = []
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.state = State(pathlib.Path(tmp.name) / 'state.json')
        for p in (patch.object(telegram.config, 'secret', side_effect=lambda k, d=None: self.secrets.get(k, d)),
                  patch.object(telegram.config, 'set_secret', side_effect=self.secrets.__setitem__)):
            p.start(); self.addCleanup(p.stop)

    def api(self, pin_ok=True):
        def call(token, method, **kw):
            self.calls.append((method, kw))
            if method == 'getUpdates':
                return {'ok': True, 'result': [{'message': {'chat': {'id': 7}, 'from': {'id': 9, 'first_name': 'A'}}}]}
            if method == 'sendMessage':
                return {'ok': True, 'result': {'message_id': len(self.calls)}}
            if method == 'pinChatMessage':
                return {'ok': pin_ok, 'description': 'not allowed'}
            return {'ok': True}
        return call

    def test_guide_is_sent_and_pinned_quietly(self):
        with patch.object(telegram, '_api', side_effect=self.api()):
            out = telegram.apply(self.state, {})
        self.assertTrue(out.ok)
        texts = [kw['text'] for m, kw in self.calls if m == 'sendMessage']
        self.assertIn('/help  This guide again.', texts[1])
        pin = next(kw for m, kw in self.calls if m == 'pinChatMessage')
        self.assertTrue(pin['disable_notification'])
        self.assertEqual(out.warnings, [])

    def test_a_failed_pin_is_a_warning_not_a_failure(self):
        with patch.object(telegram, '_api', side_effect=self.api(pin_ok=False)):
            out = telegram.apply(self.state, {})
        self.assertTrue(out.ok)
        self.assertIn('could not pin', out.warnings[0])


if __name__ == '__main__':
    unittest.main()
