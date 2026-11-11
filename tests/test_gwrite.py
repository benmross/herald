"""A source link is optional metadata, not a reason to stop calendar syncing."""

from __future__ import annotations

import copy
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import calsync, gwrite  # noqa: E402


def event(url):
    return gwrite.build_event(summary="Conference", start="2026-12-01",
                              end=None, all_day=True, sync_key="feed:conference",
                              source_url=url)


class SourceURLs(unittest.TestCase):
    def test_bare_and_protocol_relative_links_become_https(self):
        for url in ("events.example.org/conference", "//events.example.org/conference",
                    "  events.example.org/conference  "):
            with self.subTest(url=url):
                self.assertEqual(event(url)["source"]["url"],
                                 "https://events.example.org/conference")

    def test_absolute_links_preserve_paths_queries_and_fragments(self):
        for url in ("http://example.org/event.",
                    "https://example.org:8443/event?q=a%2Fb&sig=A+B#details",
                    "https://[::1]/event"):
            with self.subTest(url=url):
                self.assertEqual(event(url)["source"]["url"], url)

    def test_invalid_optional_links_do_not_remove_the_event(self):
        for url in (None, "", " ", 42, {}, "not a link", "/event", "event",
                    "mailto:events@example.org", "javascript:alert(1)",
                    "ftp://example.org/event", "https:///event", "https://",
                    "https://example.org:bad/event", "https://example.org:99999/",
                    "https://[broken/", "https://bad..example.org/event",
                    "https://example.org/a\nb", "https://example.org/a b",
                    "https://example.org/\x00", "https://example.org/\\event",
                    "https://user:password@example.org/event"):
            with self.subTest(url=url):
                body = event(url)
                self.assertNotIn("source", body)
                self.assertEqual(body["summary"], "Conference")
                self.assertEqual(body["extendedProperties"]["private"]["syncKey"],
                                 "feed:conference")

    def test_write_boundary_covers_insert_update_and_patch(self):
        for method in ("insert", "update", "patch"):
            for source, expected in (({"url": "events.example.org/event", "title": "Page"},
                                      {"url": "https://events.example.org/event", "title": "Page"}),
                                     ({"url": "mailto:events@example.org"}, "omit"),
                                     ({"title": "Missing URL"}, "omit"),
                                     ("not an object", "omit"),
                                     (None, None)):
                with self.subTest(method=method, source=source):
                    body = {**event(None), "source": source}
                    original = copy.deepcopy(body)
                    svc = mock.Mock()
                    request = getattr(svc.events.return_value, method)
                    request.return_value.execute.return_value = {"id": "created"}
                    kwargs = dict(actor="test", cal_id="cal", cal_name="Events")
                    kwargs["patch" if method == "patch" else "body"] = body
                    if method != "insert":
                        kwargs["event_id"] = "existing"
                    if method == "patch":
                        kwargs["what"] = "updated source"
                    with mock.patch.object(gwrite, "calendar_service", return_value=svc), \
                            mock.patch.object(gwrite, "_log") as audit:
                        getattr(gwrite, "calendar_" + method)(None, **kwargs)
                    sent = request.call_args.kwargs
                    self.assertEqual(sent["sendUpdates"], "none")
                    if expected == "omit":
                        self.assertNotIn("source", sent["body"])
                    else:
                        self.assertEqual(sent["body"]["source"], expected)
                    self.assertEqual(body, original)
                    audit.assert_called_once()

    def test_bad_link_does_not_block_later_sync_operations(self):
        bad, good = event(None), event("events.example.org/event")
        # Raw bodies from callers also go through the write boundary.
        bad["source"] = {"url": "mailto:events@example.org"}
        ops = [("create", "feed:bad", None, bad, ""),
               ("create", "feed:good", None, good, "")]
        svc = mock.Mock()

        def insert(**kwargs):
            source = kwargs["body"].get("source")
            if source and not source["url"].startswith("https://"):
                raise AssertionError("invalid source would abort the sync")
            return mock.Mock(execute=lambda: {"id": "created"})

        svc.events.return_value.insert.side_effect = insert
        with mock.patch.object(gwrite, "calendar_service", return_value=svc), \
                mock.patch.object(gwrite, "_log") as audit:
            counts = calsync.apply(None, ops, actor="test", cal_id="cal", cal_name="Events")
        self.assertEqual(counts["created"], 2)
        self.assertEqual(audit.call_count, 2)

    def test_unrelated_api_errors_still_fail_the_sync(self):
        svc = mock.Mock()
        svc.events.return_value.insert.return_value.execute.side_effect = RuntimeError("offline")
        with mock.patch.object(gwrite, "calendar_service", return_value=svc), \
                mock.patch.object(gwrite, "_log") as audit:
            with self.assertRaisesRegex(RuntimeError, "offline"):
                gwrite.calendar_insert(None, actor="test", cal_id="cal", cal_name="Events",
                                       body=event("events.example.org/event"))
        audit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
