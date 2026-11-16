"""`herald setup --web --preview` never runs a step's apply."""
import pathlib
import sys
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT))
from setup import engine
from setup.web import server


class Preview(unittest.TestCase):
    def test_apply_is_never_called_and_the_next_step_is_named(self):
        keys = [s.key for s in engine.steps()]
        with patch.object(server, 'PREVIEW', True):
            for step in engine.steps():
                with patch.object(step, 'apply_fn', side_effect=AssertionError('applied')):
                    with patch.object(engine, 'by_key', return_value=step):
                        server._run_step('job-' + step.key, step.key, {})
                outcome = server._job('job-' + step.key)['outcome']
                self.assertTrue(outcome['ok'], step.key)
                i = keys.index(step.key)
                self.assertEqual(outcome['next'], keys[min(i + 1, len(keys) - 1)])


if __name__ == '__main__':
    unittest.main()
