"""A turn stopped by a usage limit is continued once, at the reset, and can be cancelled."""
import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch

from herald import limitresume


class LimitResumeTests(unittest.TestCase):
    def test_model_request_retargets_a_paused_task_without_changing_it_early(self):
        for queued in (False, True):
            with self.subTest(queued=queued), tempfile.TemporaryDirectory() as folder, \
                    patch.object(limitresume.config, "LOGS", Path(folder)):
                ts = {"session_id": "stopped", "engine": "claude", "last_ts": 10,
                      "model": "custom", "effort": "max", "work": {"request": "build it"}}
                if queued:
                    limitresume.schedule(ts, 2000, "claude", 7, 3)
                else:
                    ts["limit_choice"] = {"token": "choice", "at": 2000}
                target = {"model": "opus", "effort": "high", "fresh": True}
                limitresume.request("7:3", ts, 2000, target=target)
                self.assertEqual(list((Path(folder) / "limit-resume-requests").glob("*.json")), [])
                self.assertTrue(limitresume.import_requests({"7:3": ts}))
                self.assertFalse(limitresume.import_requests({"7:3": ts}))
                self.assertEqual(ts[limitresume.KEY]["target"], target)
                self.assertEqual(ts[limitresume.ATTEMPTS], 1)
                self.assertNotIn("limit_choice", ts)
                self.assertEqual((ts["session_id"], ts["model"]), ("stopped", "custom"))
                self.assertEqual(ts["work"]["request"], "build it")

    def test_model_request_cannot_revive_cancelled_or_changed_work(self):
        for change in ("stop", "model", "effort", "session_id", "last_ts", "engine", "choice"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder, \
                    patch.object(limitresume.config, "LOGS", Path(folder)):
                ts = {"session_id": "stopped", "engine": "claude", "last_ts": 10,
                      "model": "custom", "effort": "max", "limit_choice": {"token": "old"}}
                limitresume.request("7:3", ts, 2000, target={"model": "opus"})
                if change == "stop":
                    limitresume.cancel(ts)
                elif change == "choice":
                    ts["limit_choice"] = {"token": "new"}
                else:
                    ts[change] = 11 if change == "last_ts" else "different"
                before = json.loads(json.dumps(ts))
                self.assertFalse(limitresume.import_requests({"7:3": ts}))
                self.assertEqual(ts, before)

    def test_request_reaches_existing_topic_once(self):
        ts = {"session_id": "stopped", "engine": "claude", "last_ts": 10}
        threads = {"7:3": ts}
        with tempfile.TemporaryDirectory() as folder, patch.object(limitresume.config, "LOGS", Path(folder)):
            limitresume.request("7:3", ts, 2000.0)
            self.assertTrue(limitresume.import_requests(threads))
            self.assertFalse(limitresume.import_requests(threads))
        (key, record), = limitresume.take_due(threads, 2000.0)
        self.assertEqual((key, record["chat_id"], record["thread_id"]), ("7:3", 7, 3))

    def test_request_does_not_override_newer_work_or_existing_queue(self):
        for changed in ({"session_id": "different"}, {"last_ts": 11},
                        {"engine": "codex"}, {limitresume.KEY: {"at": 3000}}):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as folder, \
                    patch.object(limitresume.config, "LOGS", Path(folder)):
                ts = {"session_id": "stopped", "engine": "claude", "last_ts": 10}
                limitresume.request("7:3", ts, 2000.0)
                ts.update(changed)
                before = dict(ts)
                self.assertFalse(limitresume.import_requests({"7:3": ts}))
                self.assertEqual(ts, before)

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
