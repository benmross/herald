"""Offline tests for the three ways a healthy turn used to get killed.

All three failures happened for real on 9 Sep 2026 and all three had the same
shape: something that is not the turn -- bookkeeping, a checkpoint, a restart
issued for unrelated reasons -- was allowed to end the turn. These tests are
here so that stays fixed. No network, no model, no systemd.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import pathlib
import sqlite3
import sys
import unittest
from contextlib import redirect_stderr
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import db, think  # noqa: E402


def _load(name: str, path: pathlib.Path):
    """Import one of the extensionless scripts in bin/ as a module."""
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, str(path)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LOCKED = sqlite3.OperationalError("database is locked")


class RunBookkeepingTests(unittest.TestCase):
    """`think._record` books a run *after* the work is done, so it must never
    be able to fail the run it is recording."""

    def test_a_locked_ledger_does_not_fail_the_turn(self):
        with mock.patch.object(db, "session", side_effect=LOCKED), \
                redirect_stderr(io.StringIO()) as err:
            think._record(label="t", engine="claude", model="m")
        self.assertIn("run not recorded", err.getvalue())

    def test_a_failing_write_does_not_fail_the_turn(self):
        with mock.patch.object(db, "record_run", side_effect=LOCKED), \
                redirect_stderr(io.StringIO()) as err:
            think._record(label="t", engine="claude", model="m")
        self.assertIn("run not recorded", err.getvalue())

    def test_a_healthy_ledger_still_gets_the_row(self):
        with mock.patch.object(db, "record_run") as rec:
            think._record(label="t", engine="claude", model="m", fell_back=0)
        rec.assert_called_once()
        self.assertEqual(rec.call_args.kwargs["label"], "t")


class CheckpointTests(unittest.TestCase):
    """The checkpoint runs on the bridge's main loop. An exception there does
    not fail one checkpoint, it exits main() and kills every live turn."""

    @classmethod
    def setUpClass(cls):
        cls.tg = _load("tg_under_test", ROOT / "bin" / "herald-telegram")

    def test_a_locked_ledger_does_not_take_the_bridge_down(self):
        with mock.patch.object(self.tg, "save_state", side_effect=LOCKED), \
                redirect_stderr(io.StringIO()) as err:
            self.tg._checkpoint({"offset": 7, "threads": {}})
        self.assertIn("checkpoint skipped", err.getvalue())

    def test_a_healthy_ledger_still_persists(self):
        with mock.patch.object(self.tg, "save_state") as save:
            self.tg._checkpoint({"offset": 7, "threads": {}})
        save.assert_called_once_with({"offset": 7, "threads": {}})

    def test_offset_is_capped_at_the_oldest_update_still_in_flight(self):
        self.tg._inflight.add(4)
        self.tg._inflight.add(9)
        try:
            with mock.patch.object(self.tg, "save_state") as save:
                self.tg._checkpoint({"offset": 12, "threads": {}})
        finally:
            self.tg._inflight.discard(4)
            self.tg._inflight.discard(9)
        self.assertEqual(save.call_args.args[0]["offset"], 4)


class RestartGuardTests(unittest.TestCase):
    """A non-deferred restart of the bridge while a topic is mid-turn kills
    that turn, and if the caller is the turn, kills its own reply."""

    @classmethod
    def setUpClass(cls):
        cls.herald = _load("herald_under_test", ROOT / "bin" / "herald")

    def _args(self, **kw):
        fields = {"units": ["herald-telegram.service"], "defer": False,
                  "reason": None, "force": False}
        fields.update(kw)
        return mock.Mock(**fields)

    def test_refuses_while_a_topic_is_mid_turn(self):
        live = [{"service": "telegram", "key": "-100:107", "pid": 1, "started": 0}]
        with mock.patch.object(self.herald, "cmd_check", return_value=0), \
                mock.patch.object(self.herald.activity, "active", return_value=live), \
                mock.patch.object(self.herald.subprocess, "run") as run, \
                redirect_stderr(io.StringIO()) as err:
            rc = self.herald.cmd_restart(self._args())
        self.assertEqual(rc, 1)
        run.assert_not_called()
        self.assertIn("--defer", err.getvalue())
        self.assertIn("-100:107", err.getvalue())

    def test_force_is_the_documented_way_through(self):
        live = [{"service": "telegram", "key": "-100:107", "pid": 1, "started": 0}]
        with mock.patch.object(self.herald, "cmd_check", return_value=0), \
                mock.patch.object(self.herald.activity, "active", return_value=live), \
                mock.patch.object(self.herald.subprocess, "run",
                                  return_value=mock.Mock(returncode=0)) as run:
            rc = self.herald.cmd_restart(self._args(force=True))
        self.assertEqual(rc, 0)
        run.assert_called_once()

    def test_restarts_normally_when_nothing_is_mid_turn(self):
        with mock.patch.object(self.herald, "cmd_check", return_value=0), \
                mock.patch.object(self.herald.activity, "active", return_value=[]), \
                mock.patch.object(self.herald.subprocess, "run",
                                  return_value=mock.Mock(returncode=0)) as run:
            rc = self.herald.cmd_restart(self._args())
        self.assertEqual(rc, 0)
        run.assert_called_once()

    def test_a_brain_only_restart_is_not_blocked_by_a_telegram_turn(self):
        """The bridge is what a Telegram turn runs under; the brain is not."""
        live = [{"service": "telegram", "key": "-100:107", "pid": 1, "started": 0}]
        with mock.patch.object(self.herald, "cmd_check", return_value=0), \
                mock.patch.object(self.herald.activity, "active", return_value=live), \
                mock.patch.object(self.herald.subprocess, "run",
                                  return_value=mock.Mock(returncode=0)) as run:
            rc = self.herald.cmd_restart(
                mock.Mock(units=["herald-brain.service"], defer=False,
                          reason=None, force=False))
        self.assertEqual(rc, 0)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=1)


class RunRecording(unittest.TestCase):
    """Every invocation is metered into `runs`, and the metering is allowed to
    fail quietly -- losing a spend line must never cost a finished answer. That
    is exactly why a broken INSERT here can go unnoticed for a day: it did, on
    9 September 2026, when the Codex fallback came out and `fell_back` stayed in
    the column list as an explicit NULL against a NOT NULL constraint."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        import sqlite3
        from herald import db
        self.db = db
        self.con = sqlite3.connect(f"{self.tmp.name}/t.db")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(db.SCHEMA)

    def test_a_minimal_run_records(self):
        row_id = self.db.record_run(self.con, label="cycle:dawn", engine="claude",
                                    model="sonnet", exit_code=0)
        self.assertTrue(row_id)

    def test_the_row_is_readable_back(self):
        self.db.record_run(self.con, label="telegram", engine="claude",
                           model="opus", cost_usd=0.25, exit_code=0)
        row = self.con.execute("SELECT * FROM runs WHERE label='telegram'").fetchone()
        self.assertEqual(row["cost_usd"], 0.25)
        self.assertEqual(row["fell_back"], 0)

    def test_an_error_run_records_too(self):
        """A failed run is the one you most want in the table afterwards."""
        row_id = self.db.record_run(self.con, label="ask", engine="claude",
                                    model="sonnet", exit_code=1,
                                    error="rate limited")
        self.assertTrue(row_id)
