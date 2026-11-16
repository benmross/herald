"""One channel per message: Telegram, and ntfy only when Telegram fails.

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
    def test_telegram_carries_it_and_ntfy_stays_quiet(self):
        with mock.patch.object(notify, "telegram", return_value=True) as tg, \
                mock.patch.object(notify, "ntfy") as fallback:
            self.assertTrue(notify.push("Scout failed", "timeout"))
        self.assertIn("Scout failed", tg.call_args.args[0])
        fallback.assert_not_called()

    def test_ntfy_only_when_telegram_fails(self):
        with mock.patch.object(notify, "telegram", return_value=False), \
                mock.patch.object(notify, "ntfy", return_value=True) as fallback:
            self.assertTrue(notify.push("Scout failed", "timeout", priority="high"))
            self.assertEqual(notify.tell("Digest", "body"), "ntfy (telegram unreachable)")
        self.assertEqual(fallback.call_args_list[0].kwargs["priority"], "high")


if __name__ == "__main__":
    unittest.main()
