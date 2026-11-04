"""Scout leaves event mail alone when an extension reads it."""
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lib"))
from cycles import scout


class EventMail(unittest.TestCase):
    def test_labels_read_only_without_a_mail_events_provider(self):
        with mock.patch.object(scout.config, "get", return_value=["Campus"]):
            with mock.patch.object(scout.capabilities, "available", return_value=False):
                self.assertEqual(scout._event_mail_labels(), ["Campus"])
            with mock.patch.object(scout.capabilities, "available", return_value=True) as av:
                self.assertEqual(scout._event_mail_labels(), [])
                av.assert_called_with("mail_events")


if __name__ == "__main__":
    unittest.main()
