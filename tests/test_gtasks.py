"""Deleted tasks and successful writes must agree with the ledger, offline."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import os
import sqlite3
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from herald import db, google, gtasks, gwrite

spec = importlib.util.spec_from_file_location("gtasks_collector", ROOT / "collectors/gtasks.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


class Tasks(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(db.SCHEMA)
        self.addCleanup(self.con.close)
        self.svc = mock.Mock()
        patch = mock.patch.object(google, "service", return_value=self.svc)
        patch.start()
        self.addCleanup(patch.stop)
        self.svc.tasklists().list().execute.return_value = {
            "items": [{"id": "l1", "title": "Tasks"}]}
        self.svc.tasks().list().execute.return_value = {"items": []}
        self.svc.reset_mock()

    def seed(self, task_id="old", list_id="l1"):
        gtasks.store_task(self.con, list_id=list_id, list_name="Tasks", task={
            "id": task_id, "title": "Keep this history", "notes": "Notes",
            "due": "2026-10-25T00:00:00.000Z", "status": "needsAction"})
        self.con.commit()

    def task(self, task_id="old", list_id="l1"):
        row = self.con.execute("SELECT * FROM facts WHERE external_id=?",
                               (f"{list_id}::{task_id}",)).fetchone()
        return row, json.loads(row["data"])

    def assert_deleted(self, list_id="l1"):
        row, data = self.task(list_id=list_id)
        self.assertEqual(data["status"], "deleted")
        self.assertTrue(data["deleted"])
        self.assertEqual(row["title"], "Keep this history")
        self.assertEqual(row["body"], "Notes")

    def test_vanished_task_no_longer_open_and_history_is_kept(self):
        self.seed()
        original_id = self.task()[0]["id"]
        self.assertEqual(collector.collect(self.con), {"lists": 1, "tasks": 0, "open": 0})
        self.assert_deleted()
        self.assertEqual(self.task()[0]["id"], original_id)
        collector.collect(self.con)
        self.assert_deleted()

    def test_sparse_google_tombstone_preserves_title_and_notes(self):
        self.seed()
        self.svc.tasks().list().execute.return_value = {
            "items": [{"id": "old", "deleted": True}]}
        collector.collect(self.con)
        self.assert_deleted()
        self.assertTrue(self.svc.tasks().list.call_args.kwargs["showDeleted"])

    def test_deleted_list_does_not_leave_open_tasks(self):
        self.seed(list_id="gone-list")
        collector.collect(self.con)
        self.assert_deleted(list_id="gone-list")

    def test_tasks_and_lists_on_later_pages_are_kept(self):
        self.seed()
        self.svc.tasklists().list().execute.side_effect = [
            {"items": [{"id": "l2", "title": "Other"}], "nextPageToken": "lists2"},
            {"items": [{"id": "l1", "title": "Tasks"}]}]
        self.svc.tasks().list().execute.side_effect = [
            {"items": []}, {"items": [], "nextPageToken": "tasks2"},
            {"items": [{"id": "old", "title": "Still open", "status": "needsAction"}]}]
        counts = collector.collect(self.con)
        self.assertEqual(counts, {"lists": 2, "tasks": 1, "open": 1})
        self.assertFalse(self.task()[1]["deleted"])
        self.assertEqual(self.svc.tasks().list.call_args.kwargs["pageToken"], "tasks2")

    def test_failure_on_later_page_leaves_previous_snapshot_intact(self):
        self.seed()
        db.put_fact(self.con, "gtasks", "list", external_id="l1", title="Old title")
        self.con.commit()
        self.svc.tasks().list().execute.side_effect = [
            {"items": [{"id": "new"}], "nextPageToken": "tasks2"},
            RuntimeError("network down")]
        with self.assertRaisesRegex(RuntimeError, "network down"):
            collector.collect(self.con)
        self.assertEqual(self.task()[1]["status"], "needsAction")
        self.assertIsNone(self.con.execute(
            "SELECT id FROM facts WHERE external_id='l1::new'").fetchone())
        self.assertEqual(self.con.execute(
            "SELECT title FROM facts WHERE kind='list'").fetchone()[0], "Old title")

    def test_task_written_during_fetch_is_not_marked_deleted(self):
        def read_tasks():
            self.seed(task_id="concurrent")
            return {"items": []}
        self.svc.tasks().list().execute.side_effect = read_tasks
        collector.collect(self.con)
        self.assertEqual(self.task("concurrent")[1]["status"], "needsAction")

    def test_restored_task_becomes_open_again(self):
        self.seed()
        collector.collect(self.con)
        self.svc.tasks().list().execute.return_value = {
            "items": [{"id": "old", "title": "Restored", "status": "needsAction"}]}
        collector.collect(self.con)
        self.assertFalse(self.task()[1]["deleted"])
        self.assertEqual(self.task()[1]["status"], "needsAction")

    def test_create_immediately_stores_the_authoritative_response_and_audit(self):
        response = {"id": "new", "title": "New task", "notes": "Details",
                    "status": "needsAction", "due": "2026-10-25T00:00:00.000Z"}
        self.svc.tasks.return_value.insert.return_value.execute.return_value = response
        returned = gwrite.task_create(self.con, actor="test", list_name="Tasks",
                                     title="New task", notes="Details", due="2026-10-25")
        self.assertEqual(returned, response)
        row, data = self.task("new")
        self.assertEqual(row["title"], response["title"])
        self.assertEqual(row["body"], response["notes"])
        self.assertEqual(row["ts"], response["due"])
        self.assertEqual(data["list_id"], "l1")
        self.assertEqual(data["status"], "needsAction")
        self.assertEqual(self.con.execute("SELECT kind FROM actions").fetchone()[0], "task.create")
        self.assertFalse(self.con.in_transaction)
        self.svc.tasks().list.assert_not_called()

    def test_create_finds_list_on_later_page(self):
        self.svc.tasklists().list().execute.side_effect = [
            {"items": [], "nextPageToken": "lists2"},
            {"items": [{"id": "l1", "title": "Tasks"}]}]
        self.svc.tasks.return_value.insert.return_value.execute.return_value = {"id": "new", "title": "New"}
        gwrite.task_create(self.con, actor="test", list_name="Tasks", title="New")
        self.assertEqual(self.task("new")[1]["status"], "needsAction")

    def test_failed_insert_does_not_record_a_task_or_action(self):
        self.svc.tasks.return_value.insert.return_value.execute.side_effect = RuntimeError("insert failed")
        with self.assertRaisesRegex(RuntimeError, "insert failed"):
            gwrite.task_create(self.con, actor="test", list_name="Tasks", title="New")
        self.assertEqual(self.con.execute("SELECT count(*) FROM facts").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT count(*) FROM actions").fetchone()[0], 0)

    def test_new_list_records_explicit_request_and_is_immediately_visible(self):
        self.svc.tasklists.return_value.insert.return_value.execute.return_value = {
            "id": "new-list", "title": "Daily"}
        result = gwrite.tasklist_create(self.con, actor="test", title="Daily",
                                        user_request="make me a daily tasks list")
        self.assertEqual(result["id"], "new-list")
        self.assertEqual(self.con.execute(
            "SELECT title FROM facts WHERE kind='list'").fetchone()[0], "Daily")
        action = self.con.execute("SELECT * FROM actions").fetchone()
        self.assertEqual(action["kind"], "tasklist.create")
        self.assertEqual(action["tier"], "green")
        self.assertIn("make me a daily tasks list", action["summary"])

    def test_duplicate_list_is_refused_before_a_write(self):
        with self.assertRaisesRegex(ValueError, "already exists"):
            gwrite.tasklist_create(self.con, actor="test", title="Tasks",
                                   user_request="make a Tasks list")
        self.svc.tasklists.return_value.insert.assert_not_called()

    def test_task_completion_requires_an_explicit_statement_before_google_access(self):
        with self.assertRaisesRegex(ValueError, "do not infer"):
            gwrite.task_complete(self.con, actor="test", list_name="Tasks", task_id="old",
                                  user_statement=" ")
        google.service.assert_not_called()

    def test_new_list_requires_an_explicit_request(self):
        with self.assertRaises(ValueError):
            gwrite.tasklist_create(self.con, actor="test", title="Daily", user_request="")
        google.service.assert_not_called()

    def test_bad_task_tier_is_refused_before_google_access(self):
        for tier in ("red", "orange"):
            with self.assertRaises(ValueError):
                gwrite.task_complete(self.con, actor="test", list_name="Tasks", task_id="old",
                                      user_statement="done", tier=tier)
        google.service.assert_not_called()

    def test_cycles_cannot_complete_reopen_or_create_lists_even_with_a_quote(self):
        with mock.patch.dict(os.environ, {"HERALD_ACTOR": "dawn"}):
            for fn in (gwrite.task_complete, gwrite.task_reopen):
                with self.assertRaises(PermissionError):
                    fn(self.con, actor="session", list_name="Tasks", task_id="old",
                       user_statement="done")
            with self.assertRaises(PermissionError):
                gwrite.tasklist_create(self.con, actor="session", title="Daily", user_request="create it")
        google.service.assert_not_called()

    def test_complete_then_reopen_updates_google_and_facts_without_duplicates(self):
        self.seed()
        self.svc.tasks.return_value.get.return_value.execute.return_value = {
            "id": "old", "title": "Work", "status": "needsAction"}
        self.svc.tasks.return_value.patch.return_value.execute.return_value = {
            "id": "old", "title": "Work", "status": "completed", "completed": "now"}
        gwrite.task_complete(self.con, actor="test", list_name="Tasks", task_id="old",
                              user_statement="I finished Work")
        self.assertEqual(self.task()[1]["status"], "completed")
        self.assertEqual(self.task()[1]["completed"], "now")
        patch = self.svc.tasks.return_value.patch.call_args.kwargs
        self.assertEqual(patch["body"]["status"], "completed")
        self.assertTrue(patch["body"]["completed"])
        self.svc.tasks.return_value.get.return_value.execute.return_value = {
            "id": "old", "title": "Work", "status": "completed", "completed": "now"}
        self.svc.tasks.return_value.patch.return_value.execute.return_value = {
            "id": "old", "title": "Work", "status": "needsAction"}
        gwrite.task_reopen(self.con, actor="test", list_name="Tasks", task_id="old",
                           user_statement="reopen Work")
        self.assertEqual(self.task()[1]["status"], "needsAction")
        self.assertIsNone(self.task()[1]["completed"])
        self.assertEqual(self.svc.tasks.return_value.patch.call_args.kwargs["body"],
                         {"status": "needsAction", "completed": None})
        self.assertEqual([r[0] for r in self.con.execute("SELECT kind FROM actions")],
                         ["task.complete", "task.reopen"])

    def test_already_completed_is_a_readback_not_another_write(self):
        self.svc.tasks.return_value.get.return_value.execute.return_value = {
            "id": "old", "title": "Work", "status": "completed"}
        gwrite.task_complete(self.con, actor="test", list_name="Tasks", task_id="old",
                              user_statement="finished Work")
        self.svc.tasks.return_value.patch.assert_not_called()
        self.assertEqual(self.task()[1]["status"], "completed")
        self.assertEqual(self.con.execute("SELECT count(*) FROM actions").fetchone()[0], 0)

    def test_assigned_and_deleted_tasks_are_refused(self):
        for extra in ({"assignmentInfo": {"contextType": "DOCUMENT"}}, {"deleted": True}):
            self.svc.tasks.return_value.get.return_value.execute.return_value = {
                "id": "old", "status": "needsAction", **extra}
            with self.assertRaises((PermissionError, ValueError)):
                gwrite.task_complete(self.con, actor="test", list_name="Tasks", task_id="old",
                                      user_statement="done")
        self.svc.tasks.return_value.patch.assert_not_called()

    def test_new_task_doors_are_refused_in_private_sessions(self):
        with mock.patch.dict(os.environ, {"HERALD_PRIVATE": "1"}):
            with self.assertRaises(gwrite.private.Refused):
                gwrite.tasklist_create(self.con, actor="test", title="Daily", user_request="make it")
            for fn in (gwrite.task_complete, gwrite.task_reopen):
                with self.assertRaises(gwrite.private.Refused):
                    fn(self.con, actor="test", list_name="Tasks", task_id="old", user_statement="done")
        google.service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
