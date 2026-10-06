"""The watchdog's guard probe must not read the user's own lift as a fault."""
import importlib.machinery
import importlib.util
import os
import pathlib
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _watchdog():
    loader = importlib.machinery.SourceFileLoader(
        "herald_watchdog", str(ROOT / "bin" / "herald-watchdog"))
    spec = importlib.util.spec_from_loader("herald_watchdog", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ExpectedExit(unittest.TestCase):
    def setUp(self):
        self.wd = _watchdog()

    def _home(self, expires: float | None) -> str:
        home = tempfile.mkdtemp()
        if expires is not None:
            d = os.path.join(home, "run", "safety")
            os.makedirs(d)
            with open(os.path.join(d, "git_destructive.allow"), "w") as f:
                f.write(str(expires))
        return home

    def test_a_probe_with_no_rule_is_always_expected_to_block(self):
        self.assertEqual(self.wd._expected_exit(None), 2)

    def test_a_rule_in_force_is_expected_to_block(self):
        self.assertEqual(self.wd._expected_exit("git_destructive", self._home(None)), 2)

    def test_a_rule_the_user_lifted_is_expected_to_pass(self):
        home = self._home(time.time() + 600)
        self.assertEqual(self.wd._expected_exit("git_destructive", home), 0)

    def test_a_lift_that_has_run_out_is_expected_to_block_again(self):
        home = self._home(time.time() - 1)
        self.assertEqual(self.wd._expected_exit("git_destructive", home), 2)

    def test_every_probe_names_its_rule_or_none(self):
        for what, event, rule in self.wd.GUARD_PROBES:
            self.assertTrue(rule is None or isinstance(rule, str), what)


if __name__ == "__main__":
    unittest.main()
