"""The runtime guard blocks writes that would go unlogged, and nothing else.

A guard that blocks too little is the gap it was built to close: an unlogged
Drive upload from a runtime script. A guard that blocks too much gets routed
around or switched off, and the easiest way to block too much is to scan *text*
instead of *what is about to run* -- Herald's own sessions constantly write
journal entries, commit messages and docs that name `messages().send()`. Both
directions are tested here.
"""

from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("guard", ROOT / "tools" / "guard.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def bash(command: str, cwd: str | None = None) -> tuple[bool, str]:
    return guard.decide({"tool_name": "Bash", "tool_input": {"command": command},
                         "cwd": cwd or str(ROOT)})


class ProseIsNeverBlocked(unittest.TestCase):
    def test_a_journal_heredoc_naming_the_danger(self):
        ok, _ = bash("cat >> ledger/journal/today.md <<'J'\n"
                     "Sharing is red: permissions().create() and messages().send()\n"
                     "must go through red.py.\nJ")
        self.assertTrue(ok)

    def test_a_commit_message_naming_it(self):
        ok, _ = bash('git commit -m "files().delete() is permanent, use trash"')
        self.assertTrue(ok)

    def test_python_that_only_mentions_it_in_a_string(self):
        ok, _ = bash("python3 - <<'PY'\nprint('never call messages().send() here')\nPY")
        self.assertTrue(ok)

    def test_reads_are_allowed(self):
        ok, _ = bash("python3 - <<'PY'\n"
                     "svc.files().list(q='name=\"x\"').execute()\n"
                     "svc.users().messages().get(userId='me', id=i).execute()\nPY")
        self.assertTrue(ok)

    def test_running_the_test_suite_is_not_inspected_as_a_script(self):
        ok, _ = bash("./venv/bin/python -m unittest tests.test_policy")
        self.assertTrue(ok)


class UnloggedWritesAreBlocked(unittest.TestCase):
    def test_the_drive_upload_that_started_this(self):
        ok, why = bash("python3 - <<'PY'\n"
                       "drive.files().create(body={'name': 'r.pdf'}, media_body=m).execute()\nPY")
        self.assertFalse(ok)
        self.assertIn("gwrite.drive_create", why)

    def test_a_script_file_run_through_grun(self):
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write("from google_api import get_service\n"
                    "d = get_service('docs', 'v1')\n"
                    "d.documents().batchUpdate(documentId=i, body={'requests': r}).execute()\n")
        ok, why = bash(f"~/.claude/skills/google-workspace/scripts/grun {f.name}")
        self.assertFalse(ok)
        self.assertIn("gwrite.doc_batch_update", why)

    def test_a_dash_c_one_liner(self):
        ok, _ = bash("python3 -c \"s.events().insert(calendarId='x', body=b).execute()\"")
        self.assertFalse(ok)

    def test_after_a_cd_and_ampersands(self):
        ok, _ = bash("cd /tmp && python3 - <<'PY'\ns.tasks().insert(tasklist=l, body=b).execute()\nPY")
        self.assertFalse(ok)

    def test_the_logged_path_is_allowed(self):
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write("import sys; sys.path.insert(0, 'lib')\n"
                    "from herald import db, gwrite\n"
                    "with db.session() as con:\n"
                    "    gwrite.drive_create(con, actor='t', name='r.pdf', path='r.pdf')\n")
        ok, _ = bash(f"./venv/bin/python {f.name}")
        self.assertTrue(ok)

    def test_running_the_door_itself_is_not_a_bypass(self):
        ok, _ = bash("./venv/bin/python lib/herald/gwrite.py")
        self.assertTrue(ok)


class RedIsBlockedAndPointsAtTheTap(unittest.TestCase):
    def test_sending_mail_from_a_script(self):
        ok, why = bash("python3 - <<'PY'\n"
                       "svc.users().messages().send(userId='me', body={'raw': r}).execute()\nPY")
        self.assertFalse(ok)
        self.assertIn("red.py", why)
        self.assertIn("herald act mail-send", why)

    def test_sharing_a_file(self):
        ok, why = bash("python3 -c \"d.permissions().create(fileId=f, body=b).execute()\"")
        self.assertFalse(ok)
        self.assertIn("red", why)

    def test_red_wins_over_amber_in_the_same_script(self):
        ok, why = bash("python3 - <<'PY'\n"
                       "d.files().create(body=b).execute()\n"
                       "d.files().delete(fileId=f).execute()\nPY")
        self.assertFalse(ok)
        self.assertIn("red.py", why)


class Connectors(unittest.TestCase):
    def test_a_connector_send_is_blocked(self):
        ok, why = guard.decide({"tool_name": "mcp__claude_ai_Gmail__send_message",
                                "tool_input": {}})
        self.assertFalse(ok)
        self.assertIn("red", why)

    def test_a_connector_write_is_blocked_as_unlogged(self):
        ok, why = guard.decide({"tool_name": "mcp__claude_ai_Gmail__label_thread",
                                "tool_input": {}})
        self.assertFalse(ok)
        self.assertIn("gwrite", why)

    def test_a_connector_read_is_allowed(self):
        ok, _ = guard.decide({"tool_name": "mcp__claude_ai_Gmail__get_message",
                              "tool_input": {}})
        self.assertTrue(ok)

    def test_other_tools_pass_through(self):
        for tool in ("Read", "Write", "Edit", "Grep", "WebFetch"):
            ok, _ = guard.decide({"tool_name": tool, "tool_input": {}})
            self.assertTrue(ok, tool)


class FailsOpen(unittest.TestCase):
    def test_a_malformed_event_does_not_raise(self):
        ok, _ = guard.decide({"tool_name": "Bash", "tool_input": {"command": "python3 -c '"}})
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
