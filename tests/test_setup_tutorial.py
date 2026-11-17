"""The Telegram tour at the end of setup."""
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT))
from setup import engine, tutorial

NOWHERE = pathlib.Path('/nonexistent/setup-state.json')


class Tour(unittest.TestCase):
    def test_is_the_last_step_and_is_marked_seen_by_finishing(self):
        self.assertEqual(engine.steps()[-1].key, 'tutorial')
        with tempfile.TemporaryDirectory() as tmp:
            state = engine.State(pathlib.Path(tmp) / 's.json')
            self.assertEqual(tutorial.status(state)[0], 'todo')
            self.assertTrue(tutorial.apply(state, {}).ok)
            self.assertEqual(tutorial.status(state)[0], engine.DONE)

    def test_scenes_serialize_and_every_step_is_well_formed(self):
        with patch.object(tutorial.config, 'enabled_engines', return_value=('claude', 'codex')):
            prompt = engine.as_dict(tutorial.prompt(engine.State(NOWHERE)))
        field = prompt['fields'][0]
        self.assertEqual(field['type'], 'tutorial')
        scenes = json.loads(json.dumps(field['data']))
        self.assertGreaterEqual(len(scenes), 10)
        for sc in scenes:
            self.assertTrue(sc['title'] and sc['caption'] and sc['steps'])
            ids = {m['id'] for m in sc.get('history', []) if m.get('id')}
            for m in sc['steps']:
                self.assertIn(m['who'], ('me', 'bot', 'event'))
                if m.get('reply_to'):
                    self.assertIn(m['reply_to'], ids)
                if m.get('tap'):
                    self.assertIn(m['tap'], [b for row in m['buttons'] for b in row])
        self.assertIn('Switching provider', [s['title'] for s in scenes])

    def test_new_and_clear_are_shown_apart_with_the_bots_own_replies(self):
        scenes = {s['title']: s for s in tutorial.scenes()}
        kept = scenes['/new keeps the last few messages']['steps'][1]['text']
        cleared = scenes['/clear forgets it all']['steps'][1]['text']
        self.assertIn('last few messages', kept)
        self.assertIn('forgotten', cleared)

    def test_one_provider_has_no_switching_scene(self):
        with patch.object(tutorial.config, 'enabled_engines', return_value=('claude',)):
            self.assertNotIn('Switching provider', [s['title'] for s in tutorial.scenes()])

    def test_terminal_gets_the_guide_as_text(self):
        field = tutorial.prompt(engine.State(NOWHERE)).fields[0]
        self.assertIn('/new', field.help)


if __name__ == '__main__':
    unittest.main()
