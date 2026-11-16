"""Engine markers preserve titles and never expose private engine choices."""
import importlib.machinery
import importlib.util
import pathlib
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from herald import tgtopics


def bridge():
    loader = importlib.machinery.SourceFileLoader("topic_names_test", str(ROOT / "bin/herald-telegram"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Names(unittest.TestCase):
    def test_switch_replaces_the_suffix_without_accumulating_markers(self):
        name = "Research 🔵"
        for engine, dot in tgtopics.DOTS.items():
            name = tgtopics.marked_name(name, engine)
            self.assertEqual(name, "Research " + dot)
            self.assertEqual(tgtopics.marked_name(name, engine), name)

    def test_keeps_internal_emoji_and_fits_the_topic_name_limit(self):
        self.assertEqual(tgtopics.marked_name("Study 🟣 notes", "codex"), "Study 🟣 notes 🔵")
        self.assertEqual(len(tgtopics.marked_name("x" * 128, "kiro")), 128)

    def test_old_root_cannot_undo_a_manual_rename(self):
        ts = {}
        root_reply = {"message_thread_id": 12, "reply_to_message": {
            "message_id": 12, "forum_topic_created": {"name": "Original"}}}
        tgtopics.observe(ts, root_reply)
        self.assertEqual(ts["topic_name"], "Original")
        tgtopics.observe(ts, {"message_thread_id": 12, "forum_topic_edited": {"name": "Renamed"}})
        tgtopics.observe(ts, root_reply)
        tgtopics.observe(ts, {"message_thread_id": 12, "forum_topic_edited": {"icon_custom_emoji_id": ""}})
        self.assertEqual(ts["topic_name"], "Renamed")

    def test_unrelated_root_and_unknown_names_never_get_guessed(self):
        ts = {}
        tgtopics.observe(ts, {"message_thread_id": 12, "reply_to_message": {
            "message_id": 13, "forum_topic_created": {"name": "Other topic"}}})
        api = Mock()
        tgtopics.sync(ts, "-100123:12", api, enabled=True, default_engine="claude")
        api.assert_not_called()

    def test_failed_edit_preserves_the_known_name_for_retry(self):
        ts = {"topic_name": "Study", "engine": "kiro"}
        api = Mock(return_value={"ok": False, "error_code": 403})
        self.assertFalse(tgtopics.sync(ts, "-100123:12", api, enabled=True, default_engine="claude"))
        self.assertEqual(ts["topic_name"], "Study")
        api.return_value = {"ok": True}
        self.assertTrue(tgtopics.sync(ts, "-100123:12", api, enabled=True, default_engine="claude"))
        self.assertEqual(ts["topic_name"], "Study 🟣")
        tgtopics.sync(ts, "-100123:12", api, enabled=True, default_engine="claude")
        self.assertEqual(api.call_count, 2)

    def test_disabled_feature_and_general_topic_never_edit(self):
        api = Mock()
        for key, enabled in [("-100123:12", False), ("-100123:main", True), ("-100123:1", True)]:
            tgtopics.sync({"topic_name": "General"}, key, api, enabled=enabled, default_engine="claude")
        api.assert_not_called()


class BridgeNames(unittest.TestCase):
    def setUp(self):
        self.mod = bridge()
        self.key = "-100123:12"
        self.ts = {"topic_name": "Research", "engine": "claude"}
        self.state = {"threads": {self.key: self.ts}}

    def test_private_engine_and_renames_do_not_escape_to_saved_state(self):
        self.mod._private[self.key] = {"engine": "kiro", "topic_name": "Private"}
        with patch.object(self.mod, "api") as api:
            self.mod._sync_topic_name(self.mod._private[self.key], self.key)
            self.mod._observe_topic_name(self.state, {"chat": {"id": -100123},
                "message_thread_id": 12, "forum_topic_edited": {"name": "Private"}})
        api.assert_not_called()
        self.assertEqual(self.ts["topic_name"], "Research")

    def test_successful_noop_does_not_delay_a_later_engine_switch(self):
        self.ts["topic_name"] = "Research 🟠"
        with patch.object(self.mod.config, "get", return_value=True), \
                patch.object(self.mod.config, "default_engine", return_value="claude"), \
                patch.object(self.mod, "api", return_value={"ok": True}) as api:
            self.mod._sync_topic_name(self.ts, self.key)
            self.ts["engine"] = "codex"
            self.mod._sync_topic_name(self.ts, self.key)
        api.assert_called_once_with("editForumTopic", chat_id=-100123,
                                    message_thread_id=12, name="Research 🔵")

    def test_response_observation_only_fills_existing_nonprivate_topics(self):
        self.mod._live_state = self.state
        result = {"message_id": 40, "chat": {"id": -100123}, "message_thread_id": 12,
                  "reply_to_message": {"message_id": 12, "forum_topic_created": {"name": "Old"}}}
        with patch.object(self.mod.tgapi, "call", return_value={"ok": True, "result": result}):
            self.mod.api("sendMessage", chat_id=-100123, text="Reply")
        self.assertEqual(self.ts["topic_name"], "Research")


if __name__ == "__main__":
    unittest.main()
