"""A turn stopped by a usage limit is continued once, at the reset, and can be cancelled."""
import unittest
from unittest.mock import patch

from herald import limitresume


class LimitResumeTests(unittest.TestCase):
    def test_time_is_the_last_full_window_reset(self):
        windows = [{"used_percent": 100.0, "resets_at": 2000.0},
                   {"used_percent": 43.0, "resets_at": 9000.0},
                   {"used_percent": 97.0, "resets_at": 500.0}]
        with patch.object(limitresume, "_windows", return_value=windows):
            self.assertEqual(limitresume.when(None, "claude", 1000.0),
                             2000.0 + limitresume.GRACE_SECONDS)

    def test_no_reading_means_a_plain_retry(self):
        with patch.object(limitresume, "_windows", return_value=[]):
            self.assertEqual(limitresume.when(None, "claude", 1000.0),
                             1000.0 + limitresume.RETRY_SECONDS)

    def test_fires_once_and_only_when_due(self):
        ts = {}
        self.assertTrue(limitresume.schedule(ts, 2000.0, "claude", 7, 3))
        threads = {"7:3": ts, "other": {}}
        self.assertEqual(limitresume.take_due(threads, 1999.0), [])
        (key, record), = limitresume.take_due(threads, 2000.0)
        self.assertEqual((key, record["chat_id"], record["thread_id"]), ("7:3", 7, 3))
        self.assertEqual(limitresume.take_due(threads, 3000.0), [])

    def test_gives_up_after_repeated_limits_and_cancel_resets(self):
        ts = {}
        for _ in range(limitresume.MAX_ATTEMPTS):
            self.assertTrue(limitresume.schedule(ts, 1.0, "claude", 7, None))
        self.assertFalse(limitresume.schedule(ts, 1.0, "claude", 7, None))
        self.assertNotIn(limitresume.KEY, ts)
        limitresume.schedule(ts, 1.0, "claude", 7, None)
        self.assertTrue(limitresume.cancel(ts))
        self.assertFalse(limitresume.cancel(ts))
        self.assertEqual(ts, {})


if __name__ == "__main__":
    unittest.main()
