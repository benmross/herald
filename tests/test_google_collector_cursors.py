"""Partial Google reads must be retried from the last complete cursor.

Exercise the collector runner and a real temporary ledger: successful rows
survive a partial run, while the cursor only moves after recovery.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "collectors"))

import gcal
import gmail
from herald import collector, config, db


class CursorRecovery(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.patch(config, "DB_PATH", pathlib.Path(tmp.name) / "facts.db")
        self.patch(collector.notify, "tell", mock.Mock())

    def patch(self, obj, name, value):
        patcher = mock.patch.object(obj, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed(self, name, cursor):
        with db.session() as con:
            db.mark_collector(con, name, ok=True, cursor=cursor)

    def saved_cursor(self, name):
        with db.session() as con:
            return collector.cursor(con, name)

    def message(self, mid):
        return {"id": mid, "threadId": mid, "internalDate": "1000000",
                "payload": {"mimeType": "text/plain", "headers": []}}

    def gmail_service(self, failure):
        svc = mock.Mock()
        svc.users().labels().list().execute.return_value = {"labels": []}
        svc.users().getProfile().execute.return_value = {
            "emailAddress": "owner@example.com", "historyId": "200"}
        svc.users().messages().list().execute.return_value = {
            "messages": [{"id": "good"}, {"id": "retry"}]}

        def get(**kwargs):
            mid = kwargs["id"]

            def execute():
                if mid == "retry" and failure[0]:
                    raise RuntimeError("429 quota exceeded")
                return self.message(mid)

            return mock.Mock(execute=execute)

        svc.users().messages().get.side_effect = get

        def batch(callback):
            requests = []

            def execute():
                for request, mid in requests:
                    try:
                        result = request.execute()
                    except Exception as error:
                        callback(mid, None, error)
                    else:
                        callback(mid, result, None)

            return mock.Mock(add=lambda req, request_id: requests.append((req, request_id)),
                             execute=execute)

        svc.new_batch_http_request.side_effect = batch
        return svc

    def check_gmail_recovery(self, previous):
        if previous:
            self.seed("gmail", previous)
        failing = [True]
        svc = self.gmail_service(failing)
        self.patch(gmail.google, "service", mock.Mock(return_value=svc))
        self.patch(gmail.time, "sleep", lambda _: None)
        history = mock.Mock(return_value=(["good", "retry"], "200"))
        self.patch(gmail, "_changed_since", history)

        self.assertEqual(collector.run("gmail", gmail.collect), 0)
        self.assertEqual(self.saved_cursor("gmail"), previous)
        with db.session() as con:
            ids = {r[0] for r in con.execute(
                "SELECT external_id FROM facts WHERE source='gmail' AND kind='message'")}
            state = con.execute("SELECT * FROM collector_state WHERE collector='gmail'").fetchone()
            self.assertEqual(state['sync_status'], 'partial')
            self.assertEqual(state['pending_retries'], 1)
            self.assertIsNone(state['last_complete']) if not previous else self.assertIsNotNone(state['last_complete'])
        self.assertEqual(ids, {"good"})

        failing[0] = False
        self.assertEqual(collector.run("gmail", gmail.collect), 0)
        self.assertEqual(self.saved_cursor("gmail"), "200")
        with db.session() as con:
            count = con.execute(
                "SELECT count(*) FROM facts WHERE source='gmail' AND kind='message'").fetchone()[0]
            state = con.execute("SELECT * FROM collector_state WHERE collector='gmail'").fetchone()
            self.assertEqual(state['sync_status'], 'complete')
            self.assertEqual(state['pending_retries'], 0)
            self.assertIsNotNone(state['last_complete'])
        self.assertEqual(count, 2)
        if previous:
            self.assertEqual([call.args[1] for call in history.call_args_list],
                             [previous, previous])
        else:
            history.assert_not_called()

    def test_gmail_retries_failed_incremental_message(self):
        self.check_gmail_recovery("100")

    def test_gmail_partial_first_run_repeats_full_listing(self):
        self.check_gmail_recovery(None)

    def check_calendar_recovery(self, previous):
        if previous:
            self.seed("gcal", previous)
        svc = mock.Mock()
        svc.calendarList().list().execute.return_value = {"items": [
            {"id": "a", "summary": "A"}, {"id": "b", "summary": "B"}]}
        failing = [True]

        def listing(**kwargs):
            def execute():
                if kwargs["calendarId"] == "a":
                    if kwargs["pageToken"] is None:
                        return {"items": [{"id": "good", "summary": "Saved first page"}],
                                "nextPageToken": "page2"}
                    if failing[0]:
                        raise RuntimeError("503 unavailable")
                    return {"items": [{"id": "deleted", "status": "cancelled"}]}
                return {"items": [{"id": "other", "summary": "Other calendar"}]}

            return mock.Mock(execute=execute)

        svc.events().list.side_effect = listing
        self.patch(gcal.google, "service", mock.Mock(return_value=svc))
        self.patch(gcal.config, "get", lambda key, default=None: default)
        with db.session() as con:
            db.put_fact(con, "gcal", "event", external_id="a::deleted", title="Stale")

        self.assertEqual(collector.run("gcal", gcal.collect), 0)
        self.assertEqual(self.saved_cursor("gcal"), previous)
        with db.session() as con:
            self.assertEqual(con.execute(
                "SELECT count(*) FROM facts WHERE source='gcal' AND kind='event'").fetchone()[0], 3)
            state = con.execute("SELECT * FROM collector_state WHERE collector='gcal'").fetchone()
            self.assertEqual(state['sync_status'], 'partial')
            self.assertEqual(state['pending_retries'], 1)

        failing[0] = False
        self.assertEqual(collector.run("gcal", gcal.collect), 0)
        self.assertIsNotNone(self.saved_cursor("gcal"))
        self.assertNotEqual(self.saved_cursor("gcal"), previous)
        with db.session() as con:
            ids = {r[0] for r in con.execute(
                "SELECT external_id FROM facts WHERE source='gcal' AND kind='event'")}
        self.assertEqual(ids, {"a::good", "b::other"})
        if previous:
            expected = (dt.datetime.fromisoformat(previous)
                        - dt.timedelta(minutes=gcal.OVERLAP_MINUTES))
            for call in svc.events().list.call_args_list:
                self.assertEqual(call.kwargs["updatedMin"],
                                 expected.isoformat().replace("+00:00", "Z"))
                self.assertTrue(call.kwargs["showDeleted"])
        else:
            for call in svc.events().list.call_args_list:
                self.assertNotIn("updatedMin", call.kwargs)

    def test_calendar_failed_later_page_preserves_shared_cursor(self):
        self.check_calendar_recovery("2026-01-01T12:00:00+00:00")

    def test_calendar_partial_first_run_repeats_full_listing(self):
        self.check_calendar_recovery(None)


if __name__ == "__main__":
    unittest.main()
