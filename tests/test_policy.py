"""The tier classifier has to tell code from prose, and err toward red.

Two failures shaped this file.

The first detector was a regex over text, and on the day it was written it
flagged its own documentation: a heredoc of prose explaining why
`permissions().create()` is dangerous matched as a real sharing call. A guard
that fires on descriptions of the danger blocks every session that writes about
it, which is how a safety control gets switched off. So the prose cases below
are as important as the code cases.

The second is the direction of error. A classifier for a safety control should
be wrong in one direction only: an unknown write is amber, never green, and
anything ambiguous between amber and red is red.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import policy  # noqa: E402


def _tiers(src: str) -> list[tuple[str, str, str]]:
    return [(c.resource, c.method, c.tier) for c in policy.find_calls(src)]


class CodeNotProse(unittest.TestCase):
    def test_a_real_call_is_found(self):
        src = "svc.permissions().create(fileId=f, body={'role': 'reader'}).execute()"
        self.assertEqual(_tiers(src), [("permissions", "create", "red")])

    def test_the_call_named_in_a_string_is_not(self):
        """The exact false positive that motivated AST detection."""
        src = ('note = """Sharing is the unguarded red action: '
               'permissions().create() makes a file visible."""\nprint(note)')
        self.assertEqual(policy.find_calls(src), [])

    def test_the_call_named_in_a_comment_is_not(self):
        src = "# never call messages().send() from here\nx = 1"
        self.assertEqual(policy.find_calls(src), [])

    def test_an_fstring_mentioning_it_is_not(self):
        src = 'msg = f"blocked {name}: files().delete() is permanent"'
        self.assertEqual(policy.find_calls(src), [])

    def test_a_fragment_that_does_not_parse_still_ignores_strings(self):
        src = ('if ok:\n    svc.messages().send(userId="me", body=b)\n'
               '    note = "and files().delete() would be worse"\n  broken indent')
        tiers = _tiers(src)
        self.assertIn(("messages", "send", "red"), tiers)
        self.assertNotIn(("files", "delete", "red"), tiers)

    def test_a_fragment_that_tokenizes_but_does_not_parse_still_catches_red(self):
        """The dangerous direction. When tokenizing succeeds the fallback used
        to rejoin tokens with spaces, which its regex could not match, so a red
        call in a fragment like this one was silently missed."""
        src = "svc.users().messages().send(userId=me, body=b).execute()\nelse:\n    pass"
        self.assertIn(("messages", "send", "red"), _tiers(src))

    def test_chained_calls_across_lines_are_found(self):
        src = ("svc = get_service('gmail', 'v1')\n"
               "(svc.users()\n    .messages()\n    .send(userId='me', body=b)\n    .execute())")
        self.assertEqual(_tiers(src), [("messages", "send", "red")])

    def test_unrelated_apis_are_ignored(self):
        src = "requests.session().get(url)\nfoo.bar().send(x)"
        self.assertEqual(policy.find_calls(src), [])


class Classification(unittest.TestCase):
    def test_reading_is_green(self):
        for res, meth in (("events", "list"), ("files", "get"), ("messages", "get"),
                          ("files", "export_media")):
            self.assertEqual(policy.classify_google(res, meth), "green", (res, meth))

    def test_reaching_a_person_is_red(self):
        for res, meth in (("messages", "send"), ("drafts", "send"),
                          ("permissions", "create"), ("permissions", "update")):
            self.assertEqual(policy.classify_google(res, meth), "red", (res, meth))

    def test_bypassing_the_trash_is_red(self):
        for res, meth in (("files", "delete"), ("messages", "delete"),
                          ("threads", "delete"), ("messages", "batchDelete"),
                          ("files", "emptyTrash")):
            self.assertEqual(policy.classify_google(res, meth), "red", (res, meth))

    def test_writes_that_redirect_or_share_by_construction_are_red(self):
        """An ACL entry shares a calendar; a filter or forwarding address quietly
        sends future mail elsewhere. Harmless-looking method names, red effects."""
        for res, meth in (("acl", "insert"), ("filters", "create"),
                          ("forwardingAddresses", "create"), ("delegates", "create")):
            self.assertEqual(policy.classify_google(res, meth), "red", (res, meth))

    def test_drafts_are_green_because_nobody_sees_them(self):
        self.assertEqual(policy.classify_google("drafts", "create"), "green")

    def test_ordinary_writes_are_amber(self):
        for res, meth in (("events", "insert"), ("files", "create"),
                          ("documents", "batchUpdate"), ("messages", "trash"),
                          ("messages", "modify")):
            self.assertEqual(policy.classify_google(res, meth), "amber", (res, meth))

    def test_an_unknown_method_on_a_known_resource_is_never_green(self):
        self.assertEqual(policy.classify_google("files", "someNewMethod"), "amber")


class Connectors(unittest.TestCase):
    def test_sending_verbs_are_red(self):
        for name in ("mcp__claude_ai_Gmail__send_message",
                     "mcp__claude_ai_Gmail__forward", "mcp__claude_ai_Gmail__reply",
                     "mcp__drive__share_file"):
            self.assertEqual(policy.classify_mcp(name), "red", name)

    def test_reading_and_drafting_are_green(self):
        for name in ("mcp__claude_ai_Gmail__get_message",
                     "mcp__claude_ai_Gmail__search_threads",
                     "mcp__claude_ai_Gmail__list_drafts",
                     "mcp__claude_ai_Gmail__create_draft",
                     "mcp__claude_ai_Gmail__update_draft"):
            self.assertEqual(policy.classify_mcp(name), "green", name)

    def test_other_writes_are_amber(self):
        for name in ("mcp__claude_ai_Gmail__label_thread",
                     "mcp__claude_ai_Gmail__trash_message",
                     "mcp__claude_ai_Gmail__delete_label",
                     "mcp__claude_ai_Gmail__mark_thread_spam"):
            self.assertEqual(policy.classify_mcp(name), "amber", name)

    def test_an_unrecognised_verb_is_amber_not_green(self):
        self.assertEqual(policy.classify_mcp("mcp__x__frobnicate"), "amber")

    def test_non_connector_tools_are_not_classified(self):
        self.assertIsNone(policy.classify_mcp("Bash"))


class NestedResources(unittest.TestCase):
    """The client library nests resources: svc.users().messages().send(),
    svc.people().connections().list(). The first classifier read the accessor
    in the middle of a chain as if it were the method, so people().connections()
    came out as an amber write, and a read-only contacts collector failed
    `herald check` on a clean checkout. Found by the bare-checkout test on
    12 Sep 2026."""

    def test_a_nested_read_is_not_a_write(self):
        src = ("svc.people().connections().list(resourceName='people/me', "
               "pageSize=100).execute()")
        self.assertEqual(policy.writes(src), [])

    def test_an_accessor_saved_to_a_variable_is_not_a_write(self):
        src = ("c = svc.people().connections()\n"
               "rows = c.list(resourceName='people/me').execute()")
        self.assertEqual(policy.writes(src), [])

    def test_a_nested_write_is_still_amber(self):
        self.assertEqual(
            _tiers("svc.spreadsheets().values().update(spreadsheetId=s, range=r, "
                   "body=b).execute()"),
            [("values", "update", "amber")])
        self.assertEqual(
            _tiers("svc.contactGroups().members().modify(resourceName=g, "
                   "body=b).execute()"),
            [("members", "modify", "amber")])

    def test_a_deeply_nested_redirect_is_red(self):
        self.assertEqual(
            _tiers("svc.users().settings().filters().create(userId='me', "
                   "body=b).execute()"),
            [("filters", "create", "red")])

    def test_a_zero_argument_red_call_is_not_mistaken_for_an_accessor(self):
        """emptyTrash takes no arguments and makes every earlier trash
        permanent. Treating every zero-argument call as navigation would miss
        exactly this."""
        self.assertEqual(_tiers("drive.files().emptyTrash().execute()"),
                         [("files", "emptyTrash", "red")])

    def test_the_fragment_fallback_knows_accessors_too(self):
        src = ("if x:\n    svc.people().connections().list(resourceName=r)\n"
               "  bad indent\nsvc.contactGroups().members().modify(resourceName=g, body=b)")
        writes = [t for t in _tiers(src) if t[2] != "green"]
        self.assertNotIn(("people", "connections", "amber"), writes)
        self.assertIn(("members", "modify", "amber"), writes)


class Tiers(unittest.TestCase):
    def test_worst_picks_the_highest(self):
        self.assertEqual(policy.worst(["green", "amber"]), "amber")
        self.assertEqual(policy.worst(["amber", "red", "green"]), "red")
        self.assertEqual(policy.worst([]), "green")

    def test_an_invented_tier_is_rejected(self):
        with self.assertRaises(ValueError):
            policy.validate_tier("orange")


if __name__ == "__main__":
    unittest.main()
