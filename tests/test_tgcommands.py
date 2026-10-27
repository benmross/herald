"""Extension queries cannot replace built-ins, invoke an engine or approve actions."""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import pathlib
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from herald import db, extensions, tgcommands


def bridge():
    loader = importlib.machinery.SourceFileLoader("tg_commands_test", str(ROOT / "bin/herald-telegram"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Registry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.enabled = []
        for patch in (mock.patch.object(tgcommands, "_registry", None),
                      mock.patch.object(extensions, "enabled", lambda: self.enabled)):
            patch.start()
            self.addCleanup(patch.stop)

    def extension(self, name, source):
        path = pathlib.Path(self.tmp.name) / name
        path.mkdir()
        (path / "telegram_commands.py").write_text(source)
        ext = types.SimpleNamespace(name=name, path=path)
        self.enabled.append(ext)
        return ext

    def test_enabled_extension_registers_and_loads_only_once(self):
        ext = self.extension("tasks", "def tasks(con, args): return 'Tasks: ' + args\nCOMMANDS={'/tasks': tasks}\n")
        commands = tgcommands.commands(("/new",))
        self.assertEqual(commands["/tasks"].handler(None, "today"), "Tasks: today")
        (ext.path / "telegram_commands.py").write_text("raise RuntimeError('do not reload')")
        self.assertEqual(tgcommands.commands()["/tasks"], commands["/tasks"])

    def test_disabled_extensions_are_not_imported(self):
        self.extension("disabled", "raise AssertionError('must not load')\n")
        self.enabled.clear()
        self.assertEqual(tgcommands.commands(), {})

    def test_builtins_and_callback_names_cannot_be_registered(self):
        self.extension("badnames", "COMMANDS={'/new': str, '/compact': str, 'approve:1': str, '/UPPER': str, '/broken': 4}\n")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tgcommands.commands(("/new", "/compact")), {})

    def test_collisions_disable_both_commands_instead_of_picking_an_extension(self):
        self.extension("one", "COMMANDS={'/tasks': str}\n")
        self.extension("two", "COMMANDS={'/tasks': str}\n")
        self.extension("three", "COMMANDS={'/tasks': str}\n")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tgcommands.commands(), {})

    def test_broken_extension_does_not_disable_other_commands(self):
        self.extension("broken", "raise RuntimeError('cannot import')\n")
        self.extension("working", "COMMANDS={'/tasks': str}\n")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIn("/tasks", tgcommands.commands())


class Dispatch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tg = bridge()

    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(db.SCHEMA)
        self.addCleanup(self.con.close)
        self.handler = mock.Mock(return_value="Your tasks")
        self.commands = {"/tasks": tgcommands.Command("tasks", self.handler)}
        self.update = {"update_id": 1, "message": {"text": "/tasks@my_bot today",
            "chat": {"id": -100}, "from": {"id": 42}, "message_thread_id": 5}}

        @contextlib.contextmanager
        def session():
            yield self.con

        for patch in (mock.patch.object(tgcommands, "commands", lambda *a: self.commands),
                      mock.patch.object(self.tg.config, "secret", return_value=42),
                      mock.patch.object(self.tg.db, "session", session),
                      mock.patch.object(self.tg, "send"),
                      mock.patch.object(self.tg, "handle"),
                      mock.patch.object(self.tg.think, "think"),
                      mock.patch.object(self.tg, "_checkpoint"),
                      mock.patch.object(self.tg, "_maybe_restart")):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.tg._private.clear)

    def test_command_answers_in_its_topic_without_a_model_turn(self):
        self.tg._dispatch(self.update, {})
        self.handler.assert_called_once_with(self.con, "today")
        self.tg.send.assert_called_once_with(-100, "Your tasks", 5, markup=False)
        self.tg.handle.assert_not_called()
        self.tg.think.think.assert_not_called()
        self.assertEqual(self.tg.unknown_command("/tasks"), "")

    def test_another_user_cannot_invoke_the_extension(self):
        self.update["message"]["from"]["id"] = 99
        self.assertFalse(self.tg._handle_extension_command(self.update, "-100:5"))
        self.handler.assert_not_called()
        self.tg.send.assert_not_called()

    def test_private_topics_do_not_invoke_handlers(self):
        self.tg._private["-100:5"] = {}
        self.assertTrue(self.tg._handle_extension_command(self.update, "-100:5"))
        self.handler.assert_not_called()
        self.assertIn("private session", self.tg.send.call_args.args[1])

    def test_connection_refuses_writes_and_failure_never_falls_back_to_a_model(self):
        self.handler.side_effect = lambda con, args: con.execute("DELETE FROM approvals")
        with contextlib.redirect_stderr(io.StringIO()):
            self.tg._dispatch(self.update, {})
        self.assertIn("failed", self.tg.send.call_args.args[1])
        self.tg.handle.assert_not_called()
        self.tg.think.think.assert_not_called()
        self.assertEqual(self.con.execute("PRAGMA query_only").fetchone()[0], 0)

    def test_builtin_command_does_not_invoke_an_extension(self):
        self.update["message"]["text"] = "/new"
        self.commands["/new"] = tgcommands.Command("bad", self.handler)
        self.assertFalse(self.tg._handle_extension_command(self.update, "-100:5"))
        self.handler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
