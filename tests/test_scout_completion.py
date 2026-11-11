"""Scout must finish after a successful model turn, with no real side effects."""

import contextlib
import copy
import io
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lib"))

from cycles import scout
from herald import context, db, gwrite, think


def payload():
    return {"summary": "Reviewed the new candidates.", "opened": 0, "rejected": 0,
            "posting_decisions": [
                {"review_id": "settled", "decision": "rejected", "reason": "Requires a doctorate."},
                {"review_id": "deferred", "decision": "deferred", "reason": "Page unavailable."},
            ], "mail_readings": []}


class Completion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name)
        self.con = sqlite3.connect(self.path / "facts.db")
        self.con.row_factory = sqlite3.Row
        self.addCleanup(self.con.close)
        self.con.executescript(db.SCHEMA)
        for key in ("settled", "deferred", "held-back"):
            db.put_fact(self.con, "internships", "posting", external_id=key, title=key)
        db.put_fact(self.con, "opportunities", "candidate", external_id="mail", title="Mail")
        self.con.commit()
        self.out, self.err = io.StringIO(), io.StringIO()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(scout.db, "session", self.session))
        stack.enter_context(mock.patch.object(scout.config, "RAW", self.path))
        stack.enter_context(mock.patch.object(scout, "build_snapshot", return_value=(
            "Test snapshot", {"settled", "deferred", "mail"})))
        stack.enter_context(mock.patch.object(scout, "_event_mail_labels", return_value=[]))
        stack.enter_context(mock.patch.object(context, "refresh_opportunities"))
        self.model = stack.enter_context(mock.patch.object(scout.think, "think"))
        self.push = stack.enter_context(mock.patch.object(scout.notify, "push"))
        self.tell = stack.enter_context(mock.patch.object(scout.notify, "tell"))
        stack.enter_context(mock.patch.dict(os.environ))
        stack.enter_context(contextlib.redirect_stdout(self.out))
        stack.enter_context(contextlib.redirect_stderr(self.err))

    @contextlib.contextmanager
    def session(self):
        with self.con:
            yield self.con

    def test_success_reaches_final_summary_and_preserves_unreviewed_candidates(self):
        self.model.return_value = think.Result(ok=True, engine="test", structured=payload())
        self.assertEqual(scout.main(), 0)
        self.assertIn("scout: 4 new (3 shown)", self.out.getvalue())
        self.assertIn("Reviewed the new candidates.", self.out.getvalue())
        self.assertEqual(scout._seen(self.con), {"mail", "settled"})
        self.assertEqual(self.con.execute(
            "SELECT count(*) FROM facts WHERE source='scout_review'").fetchone()[0], 2)
        self.model.assert_called_once()
        self.push.assert_not_called()
        self.tell.assert_not_called()

    def test_valid_notification_can_be_formatted_and_completion_still_succeeds(self):
        reply = payload()
        reply["push_now"] = [{"title": "Conference", "why": "Applications close tomorrow.",
                              "url": "https://example.org/", "deadline": "2026-12-01"}]
        self.model.return_value = think.Result(ok=True, engine="test", structured=reply)
        self.assertEqual(scout.main(), 0)
        self.tell.assert_called_once()
        self.assertIn("Applications close tomorrow.", self.tell.call_args.args[1])

    def test_invalid_reply_fails_cleanly_without_consuming_candidates_or_notifying_finds(self):
        missing_title = payload()
        missing_title["push_now"] = [{"why": "Tomorrow"}]
        bad_decision = payload()
        bad_decision["posting_decisions"] = [{"review_id": [], "decision": "rejected", "reason": "No"}]
        bad_reading = payload()
        bad_reading["mail_readings"] = [None]
        for reply in (None, [], "plain text", {}, missing_title, bad_decision, bad_reading):
            with self.subTest(reply=reply):
                self.model.return_value = think.Result(ok=True, structured=reply)
                self.assertEqual(scout.main(), 1)
                self.assertEqual(scout._seen(self.con), set())
                self.assertEqual(self.con.execute(
                    "SELECT count(*) FROM facts WHERE source='scout_review'").fetchone()[0], 0)
        self.assertIn("Invalid Scout output", self.err.getvalue())
        self.tell.assert_not_called()

    def test_model_failure_is_reported_without_consuming_candidates(self):
        self.model.return_value = think.Result(ok=False, error="Engine unavailable")
        self.assertEqual(scout.main(), 1)
        self.assertEqual(scout._seen(self.con), set())
        self.push.assert_called_once()
        self.tell.assert_not_called()

    def test_wrongly_shaped_seen_cache_does_not_break_sorting_on_completion(self):
        for saved in (None, 7, {}, ["mail", {}, ["nested"], 12]):
            with self.subTest(saved=saved):
                self.con.execute("DELETE FROM collector_state WHERE collector='scout'")
                self.con.execute("INSERT INTO collector_state(collector,cursor) VALUES('scout',?)",
                                 (json.dumps(saved),))
                scout._remember(self.con, scout._seen(self.con) | {"settled"})
                self.assertTrue(all(isinstance(key, str) for key in scout._seen(self.con)))


class Payload(unittest.TestCase):
    def test_null_optionals_from_strict_schema_are_omitted_without_changing_reply(self):
        reply = payload()
        reply.update(push_now=None, calendar_events=[{
            "title": "Conference", "start": "2026-12-01", "end": None,
            "all_day": None, "description": None,
        }])
        original = copy.deepcopy(reply)
        clean = scout._validated_payload(reply)
        self.assertNotIn("push_now", clean)
        self.assertNotIn("end", clean["calendar_events"][0])
        self.assertEqual(reply, original)

    def test_invalid_scalar_types_and_decisions_are_rejected(self):
        for field, value in (("summary", []), ("opened", True), ("rejected", "zero")):
            reply = payload()
            reply[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                scout._validated_payload(reply)
        reply = payload()
        reply["posting_decisions"][0]["decision"] = "unknown"
        with self.assertRaises(ValueError):
            scout._validated_payload(reply)


class CalendarInput(unittest.TestCase):
    def test_malformed_event_does_not_block_valid_later_events(self):
        valid = {"title": "Conference", "start": "2026-12-01T18:00:00-05:00"}
        events = [
            {"title": "Bad date", "start": "not a date"},
            {"title": "Bad end", "start": "2026-12-01", "end": "TBA"},
            {"title": "Missing offset", "start": "2026-12-01T18:00:00"},
            {"title": "Reversed", "start": "2026-12-01T18:00:00-05:00",
             "end": "2026-12-01T17:00:00-05:00"},
            {"title": "Bad type", "start": 7}, valid, valid,
            {"title": "Date overflow", "start": "9999-12-31"},
            {"title": "Time overflow", "start": "9999-12-31T23:30:00+00:00"},
        ]
        with mock.patch.object(gwrite, "calendar_id", return_value="cal"), \
                mock.patch.object(gwrite, "calendar_window", return_value=[]), \
                mock.patch.object(gwrite, "calendar_insert") as insert, \
                mock.patch("herald.calsync.covered_elsewhere", return_value=lambda *a: None), \
                mock.patch.object(scout.db, "session"), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(scout._write_calendar_events(events), 1)
        insert.assert_called_once()
        self.assertEqual(insert.call_args.kwargs["body"]["summary"], "Conference")

    def test_all_day_and_timed_event_shapes_still_build(self):
        for start, end, all_day in (("2026-12-01", "2026-12-02", True),
                                    ("2026-12-01T18:00:00-05:00", None, False)):
            with self.subTest(start=start):
                self.assertEqual(scout._event_times({"start": start, "end": end}),
                                 (start, end, all_day))


class MailImages(unittest.TestCase):
    def test_optional_cache_maintenance_failures_do_not_abort_the_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "scout-images").mkdir()
            with mock.patch.object(scout.config, "RAW", root), \
                    mock.patch.object(pathlib.Path, "iterdir", side_effect=OSError("unavailable")), \
                    mock.patch.object(scout.mailimages.Seen, "save", side_effect=OSError("read only")), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                reader = scout._MailReader(None, scout.dt.datetime.now(scout.config.tz()))
                reader.finish()
            self.assertIn("cleanup failed", err.getvalue())
            self.assertIn("could not be saved", err.getvalue())

    def test_optional_image_failures_leave_text_available_for_both_mail_sources(self):
        for source in ("gmail", "purelymail"):
            for error in (OSError("cache unavailable"), LookupError("unknown charset")):
                with self.subTest(source=source, error=error), tempfile.TemporaryDirectory() as tmp:
                    with mock.patch.object(scout.config, "RAW", pathlib.Path(tmp)), \
                            mock.patch.object(scout._MailReader, "images", side_effect=error), \
                            mock.patch("herald.purelymail.images", side_effect=error), \
                            contextlib.redirect_stderr(io.StringIO()):
                        reader = scout._MailReader(None, scout.dt.datetime.now(scout.config.tz()))
                        lines = reader.render({"source": source, "external_id": "message",
                                               "id": 1, "data": "{}", "body": "The event is tomorrow."})
                    self.assertIn("The event is tomorrow.", lines)
                    self.assertTrue(any("could not be fetched" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
