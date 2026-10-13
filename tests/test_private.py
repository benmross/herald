"""A private session reads and cannot save or change anything.

The read-only mount is the part that holds, and it is tested against the real
`bwrap` where there is one: a test that only inspected the argument list would
pass on a sandbox that mounted nothing. Everything a mount cannot stop is at
Herald's own doors, and each door is tested for refusing.

The failure this file is here to catch is a quiet one. Private mode that
stopped working would look exactly like private mode working, until somebody
went looking for a conversation they had been told was never kept.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import config, db, gwrite, private, red, think  # noqa: E402

_spec = importlib.util.spec_from_file_location("guard", ROOT / "tools" / "guard.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

PRIVATE = {private.ENV: "1"}


def decide(tool: str, **tool_input) -> bool:
    return guard.decide({"tool_name": tool, "tool_input": tool_input,
                         "cwd": str(ROOT)})[0]


class TheGuard(unittest.TestCase):
    def test_outside_a_private_session_nothing_changes(self):
        with mock.patch.dict(os.environ, {private.ENV: ""}):
            self.assertTrue(decide("Write", file_path="/tmp/x", content="x"))
            self.assertTrue(decide("Bash", command="git push origin main"))

    def test_the_file_tools_are_refused(self):
        with mock.patch.dict(os.environ, PRIVATE):
            for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch"):
                self.assertFalse(decide(tool, file_path="/tmp/x"), tool)

    def test_reading_is_not(self):
        with mock.patch.dict(os.environ, PRIVATE):
            self.assertTrue(decide("Read", file_path=str(ROOT / "README.md")))
            self.assertTrue(decide("Bash", command="herald db 'select 1'"))
            self.assertTrue(decide("Bash", command="git log --oneline | head"))
            self.assertTrue(decide("Bash", command="curl -s https://example.com/x"))
            self.assertTrue(decide("Bash", command="gh pr list"))
            self.assertTrue(decide("Bash", command="gh api repos/a/b/issues"))

    def test_what_lands_on_another_machine_is_refused(self):
        with mock.patch.dict(os.environ, PRIVATE):
            for command in ("ssh somehost 'rm -rf x'", "scp a somehost:b",
                            "git push origin main", "git -C /x push",
                            "curl -X POST https://example.com -d a=b",
                            "gh issue create --title x",
                            "gh api -X DELETE repos/a/b/issues/1",
                            "true && rsync -a a/ somehost:b/"):
                self.assertFalse(decide("Bash", command=command), command)

    def test_a_google_write_from_a_script_is_refused_without_pointing_at_gwrite(self):
        with mock.patch.dict(os.environ, PRIVATE):
            ok, reason = guard.decide({
                "tool_name": "Bash", "cwd": str(ROOT), "tool_input": {"command":
                    "python3 - <<'PY'\nsvc.events().insert(calendarId=c, body=b).execute()\nPY"}})
        self.assertFalse(ok)
        self.assertIn("private session", reason)
        self.assertNotIn("gwrite", reason)

    def test_a_connector_that_is_not_a_known_read_is_refused(self):
        with mock.patch.dict(os.environ, PRIVATE):
            self.assertFalse(decide("mcp__some_server__do_a_thing"))


class TheDoors(unittest.TestCase):
    def test_gwrite_refuses_before_it_reaches_google(self):
        with mock.patch.dict(os.environ, PRIVATE), \
                mock.patch.object(gwrite.google, "service") as service:
            with self.assertRaises(private.Refused):
                gwrite.task_create(None, actor="t", list_name="a", title="b")
            service.assert_not_called()

    def test_every_gwrite_write_is_wrapped_and_no_read_is(self):
        """`actor` is what marks a write. A function added without it would be
        an unlogged write, which the other gwrite tests already refuse."""
        with mock.patch.dict(os.environ, PRIVATE):
            for name in ("calendar_insert", "gmail_trash", "drive_create",
                         "sheet_batch_update", "contact_set_address"):
                with self.assertRaises(private.Refused, msg=name):
                    getattr(gwrite, name)(None, actor="t")
            self.assertEqual(gwrite.norm_title("A b"), "ab")

    def test_red_refuses(self):
        with mock.patch.dict(os.environ, PRIVATE):
            with self.assertRaises(private.Refused):
                red.request("drill", {}, actor="t")

    def test_the_database_opens_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "facts.db"
            con = sqlite3.connect(path)
            con.executescript("CREATE TABLE t (x); INSERT INTO t VALUES (1);")
            con.commit()
            con.close()
            with mock.patch.object(config, "DB_PATH", path), \
                    mock.patch.dict(os.environ, PRIVATE):
                with db.session() as con:
                    self.assertEqual(con.execute("SELECT x FROM t").fetchone()[0], 1)
                    with self.assertRaises(sqlite3.OperationalError):
                        con.execute("INSERT INTO t VALUES (2)")

    def test_herald_runs_only_its_reading_subcommands(self):
        env = {**os.environ, **PRIVATE}
        for argv in (["notify", "x"], ["think", "x"], ["collect"],
                     ["config", "set", "a.b", "1"], ["restart"]):
            r = subprocess.run([sys.executable, str(ROOT / "bin" / "herald"), *argv],
                               env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, argv)
            self.assertIn("private session", r.stderr, argv)


class NothingIsBooked(unittest.TestCase):
    def test_a_private_run_writes_no_runs_row_and_no_anomaly_log(self):
        think._private.scratch = pathlib.Path("/nonexistent")
        try:
            with mock.patch.object(think.db, "session") as session, \
                    mock.patch.object(think, "_ANOMALY_LOG") as log:
                think._record(label="telegram", engine="claude", model="m")
                think._log_anomaly("first", "telegram", "m", None, {}, "", "")
                session.assert_not_called()
                log.open.assert_not_called()
        finally:
            think._private.scratch = None

    def test_an_ordinary_run_still_is(self):
        with mock.patch.object(think.db, "session") as session:
            think._record(label="ask", engine="claude", model="m")
            session.assert_called_once()

    def test_without_a_sandbox_a_private_run_does_not_start(self):
        """Falling back to an ordinary run would be the worst outcome: the
        user would have been told it was private."""
        with mock.patch.object(private, "unavailable", return_value="no bwrap"), \
                mock.patch.object(think, "_spawn") as spawn:
            r = think.think("x", label="ask", engine="claude", private="/nonexistent")
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "no bwrap")
        spawn.assert_not_called()


class TheScratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(private, "_ram_root",
                                  return_value=pathlib.Path(self.tmp.name) / "root")
        patch.start()
        self.addCleanup(patch.stop)

    def test_destroy_removes_it_and_only_it(self):
        scratch = private.new_scratch()
        (scratch / "transcript").write_text("x")
        outside = pathlib.Path(self.tmp.name) / "keep"
        outside.mkdir()
        private.destroy(outside)
        self.assertTrue(outside.exists())
        private.destroy(scratch)
        self.assertFalse(scratch.exists())

    def test_sweep_takes_one_owner_and_leaves_the_other(self):
        mine, theirs = private.new_scratch("telegram"), private.new_scratch("once")
        self.assertEqual(private.sweep("telegram"), 1)
        self.assertFalse(mine.exists())
        self.assertTrue(theirs.exists())

    def test_engine_state_is_shadowed_and_the_login_is_not(self):
        home = pathlib.Path(self.tmp.name) / "engine"
        (home / "projects").mkdir(parents=True)
        (home / "skills").mkdir()
        for name in (".credentials.json", "history.jsonl", "settings.json", "state.sqlite"):
            (home / name).write_text("{}")
        scratch = private.new_scratch()
        args = private.wrap(["claude", "-p"], "claude", scratch,
                            {"CLAUDE_CONFIG_DIR": str(home), "PATH": ""})
        binds = {args[i + 2]: (args[i], args[i + 1]) for i, a in enumerate(args)
                 if a in ("--bind", "--ro-bind") and i + 2 < len(args)}
        self.assertEqual(binds["/"][0], "--ro-bind")
        self.assertEqual(binds[str(home)], ("--bind", str(home)))
        self.assertNotIn(str(home / ".credentials.json"), binds)
        self.assertEqual(binds[str(home / "settings.json")][0], "--ro-bind")
        self.assertEqual(binds[str(home / "skills")][0], "--ro-bind")
        for name in ("projects", "history.jsonl", "state.sqlite", "state.sqlite-wal"):
            kind, source = binds[str(home / name)]
            self.assertEqual(kind, "--bind", name)
            self.assertTrue(source.startswith(str(scratch)), name)
        self.assertEqual(args[-2:], ["claude", "-p"])


@unittest.skipUnless(shutil.which("bwrap") and private.unavailable() is None,
                     "no bubblewrap here")
class TheSandbox(unittest.TestCase):
    """The real mount. A user namespace can be refused by the host (some CI
    runners, some containers), which is a fact about that host and skips."""

    def run_inside(self, script: str) -> subprocess.CompletedProcess:
        scratch = private.new_scratch("test")
        self.addCleanup(private.destroy, scratch)
        r = subprocess.run(private.wrap(["sh", "-c", script], "claude", scratch),
                           capture_output=True, text=True)
        if r.returncode != 0 and "bwrap:" in r.stderr:
            self.skipTest(f"bwrap cannot run here: {r.stderr.strip()[:120]}")
        return r

    def test_nothing_outside_tmp_can_be_written(self):
        with tempfile.NamedTemporaryFile(dir=pathlib.Path.home(), delete=True) as real:
            target = pathlib.Path(real.name)
            r = self.run_inside(
                f"echo changed > {target} 2>/dev/null; rm {target} 2>/dev/null; "
                f"touch {target}.new 2>/dev/null; touch /tmp/ok && echo tmp; "
                f"echo ${private.ENV}")
            self.assertEqual(r.stdout.split(), ["tmp", "1"])
            self.assertTrue(target.exists())
            self.assertEqual(target.read_bytes(), b"")
            self.assertFalse(pathlib.Path(f"{target}.new").exists())

    def test_what_is_written_to_tmp_does_not_reach_the_real_one(self):
        name = f"herald-private-test-{os.getpid()}"
        self.run_inside(f"echo x > /tmp/{name}")
        self.assertFalse(pathlib.Path("/tmp", name).exists())


if __name__ == "__main__":
    unittest.main()
