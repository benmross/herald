"""One channel: Telegram. ntfy was removed on 16 Nov 2026.

`push()` used to be ntfy itself, so `herald notify` without a file and every
cycle-failure alert skipped Telegram entirely.
"""

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))
from herald import notify  # noqa: E402


class Route(unittest.TestCase):
    def test_push_goes_to_telegram_and_ignores_old_ntfy_arguments(self):
        with mock.patch.object(notify, "telegram", return_value=True) as tg:
            self.assertTrue(notify.push("Scout failed", "timeout", priority="high",
                                        tags="warning", actions=[{"label": "x"}]))
        self.assertIn("Scout failed", tg.call_args.args[0])

    def test_a_failed_telegram_is_reported_not_rerouted(self):
        with mock.patch.object(notify, "telegram", return_value=False):
            self.assertFalse(notify.push("Scout failed", "timeout"))
            self.assertEqual(notify.tell("Digest", "body"), "nowhere")
        self.assertFalse(hasattr(notify, "ntfy"))


if __name__ == "__main__":
    unittest.main()
