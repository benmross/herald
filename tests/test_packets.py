"""Application packets: answers resolved for review without a browser."""
import sys
import pathlib
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "lib"))

from herald import apply as ap  # noqa: E402
from herald import packets  # noqa: E402

BANK = {"profile": {"first_name": "Ada", "last_name": "Lovelace", "full_name": "Ada Lovelace",
                    "email": "ada@example.com", "phone": "555-010-0199", "state": "Maryland"},
        "experience": [{"title": "Analyst", "company": "Engine Works", "start": "06/2025"}],
        "education": [], "answers": [{"q": "sponsorship", "a": "No"}]}


class Answers(unittest.TestCase):
    def q(self, label, kind="text", options=None, observed=None):
        return {"key": label, "label": label, "kind": kind, "options": options or [],
                "required": True, "observed": observed}

    def resolve(self, q, answers=None):
        v, src = packets._answer(BANK, q, "Example", answers or {})
        q.update(answer=v, source=src, display=packets._display(q, v))
        q["flag"] = packets._flag(q)
        return q

    def test_profile_wins_over_an_old_per_application_copy(self):
        q = self.resolve(self.q("First Name"), {ap.norm("First Name"): "ADA"})
        self.assertEqual((q["answer"], q["source"], q["flag"]), ("Ada", "profile", "routine"))

    def test_employer_specific_answer_is_shown_for_review(self):
        q = self.resolve(self.q("Have you written Go?", "radio", ["Yes", "No"]),
                         {ap.norm("Have you written Go?"): "No"})
        self.assertEqual((q["display"], q["flag"]), ("No", "review"))

    def test_bank_answer_is_routine(self):
        q = self.resolve(self.q("Will you require sponsorship?", "radio", ["Yes", "No"]))
        self.assertEqual((q["display"], q["flag"]), ("No", "routine"))

    def test_unanswered_required_question_is_missing(self):
        self.assertEqual(self.resolve(self.q("Why us?", "textarea"))["flag"], "missing")

    def test_form_value_from_work_history_is_not_re_derived(self):
        q = self.resolve(self.q("From", "date", observed="06/2025"))
        self.assertEqual((q["answer"], q["source"]), ("06/2025", "bank"))

    def test_reformatted_profile_value_is_routine(self):
        q = self.resolve(self.q("Mobile", observed="5550100199"))
        self.assertEqual(q["flag"], "routine")


class Options(unittest.TestCase):
    def test_alternation_is_a_preference_order(self):
        want = "careers? (site|page)|(company|corporate) website|job board|other"
        self.assertEqual(ap.best_option(want, ["Job Board", "Corporate Website", "Other"]), "Corporate Website")
        self.assertEqual(ap.best_option(want, ["LinkedIn", "Other"]), "Other")

    def test_group_alternation_is_not_split(self):
        self.assertEqual(ap._top_level_branches("United States( of America)?|USA"),
                         ["United States( of America)?", "USA"])


class Urls(unittest.TestCase):
    def test_greenhouse_ids(self):
        self.assertEqual(packets._greenhouse_ids("https://job-boards.greenhouse.io/acme/jobs/123"), ("acme", "123"))
        self.assertEqual(packets._greenhouse_ids("https://acme.com/careers?gh_jid=9&for=acme"), ("acme", "9"))
        self.assertIsNone(packets._greenhouse_ids("https://jobs.ashbyhq.com/acme/x"))


if __name__ == "__main__":
    unittest.main()
