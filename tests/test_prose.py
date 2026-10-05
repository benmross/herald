"""The prose check, held to the sentences it exists for and the ones it must
leave alone.

The second half matters as much as the first. A flagged reply costs a model
round trip, so a rule that fires on an honest correction or on quoted text is
a tax on every session, and the first thing anybody would do about that is
turn the check off.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from herald import prose  # noqa: E402

HOOK = ROOT / "tools" / "prose_hook.py"


def rules(text: str) -> set[str]:
    return {h.rule for h in prose.lint(text)}


class ThePatternsItExistsFor(unittest.TestCase):
    def test_the_readme_example_from_the_thread(self):
        text = ("Here's where it gets interesting: the retry logic isn't just a "
                "nice-to-have — it's the load-bearing assumption of the sync "
                "pipeline.")
        self.assertLessEqual({"signpost", "not_just", "em_dash", "stock_phrase"},
                             rules(text))

    def test_each_hard_pattern_alone_is_enough(self):
        for text in (
            "It runs at 04:00 — every night.",
            "It's not just sourcing, it's framing.",
            "No fluff, no filler. Just results.",
            "Here's the thing about the timer.",
            "You're absolutely right, the path was wrong.",
            "That was a great question.",
            "Let me know if you want the rest.",
            "In summary, the exam is on Wednesday.",
            "That check is load-bearing.",
            "The film is known not only for its animation but also for its score.",
        ):
            with self.subTest(text=text):
                self.assertTrue(prose.flagged(prose.lint(text)))

    def test_two_soft_patterns_together_are_flagged(self):
        text = "The result? Nothing changed. The fix: restart the bridge."
        self.assertLessEqual({"question_answer", "label_colon"}, rules(text))
        self.assertFalse(any(h.weight == prose.HARD for h in prose.lint(text)))
        self.assertTrue(prose.flagged(prose.lint(text)))

    def test_a_contrast_split_over_two_sentences(self):
        self.assertIn("not_x_its_y", rules("The tax isn't the problem. The mindset is."))

    def test_fragments_written_for_rhythm(self):
        self.assertIn("staccato", rules("Smooth. Effortless. A perfect fit for you."))

    def test_a_list_of_bold_labels(self):
        text = ("- **Speed:** it is fast\n- **Cost:** it is cheap\n"
                "- **Scale:** it is big\n")
        self.assertIn("bold_list", rules(text))


class WhatItMustLeaveAlone(unittest.TestCase):
    def test_plain_replies_pass_clean(self):
        for text in (
            "Done. Pushed as 4896ed4, and the bridge restarts in five seconds.",
            "Want me to book it? The 3pm slot is still open.",
            "Your exam was today at 10. The next one is Wednesday.",
            "Is it on your calendar? No, it is on the public feed, so it says "
            "nothing about whether you are going.",
            "I can't simply delete it, because the digest reads that table.",
        ):
            with self.subTest(text=text):
                self.assertEqual(prose.lint(text), [])

    def test_one_honest_correction_is_not_flagged(self):
        hits = prose.lint("The exam isn't Wednesday, it's Thursday at 6pm.")
        self.assertFalse(prose.flagged(hits))

    def test_quoted_text_is_not_read(self):
        for text in (
            'He wrote "it\'s not just a bug — it\'s a feature" in the email.',
            "The flag is `--no-edit — really`.",
            "> Here's the kicker — load-bearing.\n\nThat is what the post said.",
            "```\nhere's the thing — delve\n```\nThat block is the sample.",
            "See https://example.org/a—b for it.",
        ):
            with self.subTest(text=text):
                self.assertEqual(prose.lint(text), [])

    def test_a_rule_can_be_switched_off(self):
        text = "It runs at 04:00 — every night."
        self.assertEqual(prose.lint(text, off=["em_dash"]), [])

    def test_line_numbers_survive_the_blanking(self):
        text = "```\ncode\n```\n\nfine\n\nThat check is load-bearing."
        self.assertEqual(prose.lint(text)[0].line, 7)


class TheHook(unittest.TestCase):
    def run_hook(self, event: dict, conf: dict | None = None):
        with tempfile.TemporaryDirectory() as home:
            if conf is not None:
                pathlib.Path(home, "config.json").write_text(json.dumps({"prose": conf}))
            env = dict(os.environ, HERALD_HOME=home)
            done = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event),
                                  capture_output=True, text=True, env=env, timeout=20)
            log = pathlib.Path(home, "ledger", "raw", "prose.log")
            return done, (log.read_text() if log.exists() else "")

    def test_a_flagged_reply_is_sent_back_with_the_list(self):
        done, log = self.run_hook(
            {"last_assistant_message": "Here's the thing — it works."})
        self.assertEqual(done.returncode, 2)
        self.assertIn("em_dash", done.stderr)
        self.assertIn("signpost", done.stderr)
        self.assertIn("FLAG", log)

    def test_a_clean_reply_ends_the_turn(self):
        done, log = self.run_hook({"last_assistant_message": "It works now."})
        self.assertEqual(done.returncode, 0)
        self.assertEqual(log, "")

    def test_it_never_refuses_the_rewrite(self):
        """Twice would be a loop: a reply that cannot pass could never be sent."""
        done, log = self.run_hook({"last_assistant_message": "Still — this.",
                                   "stop_hook_active": True})
        self.assertEqual(done.returncode, 0)
        self.assertIn("REWROTE still-flagged", log)

    def test_warn_mode_logs_and_lets_it_through(self):
        done, log = self.run_hook({"last_assistant_message": "Still — this."},
                                  conf={"mode": "warn"})
        self.assertEqual(done.returncode, 0)
        self.assertIn("WARN", log)

    def test_off_means_off(self):
        done, log = self.run_hook({"last_assistant_message": "Still — this."},
                                  conf={"mode": "off"})
        self.assertEqual((done.returncode, log), (0, ""))

    def test_the_reply_is_found_in_a_transcript(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "That was a great question."}]}}) + "\n")
        try:
            done, _ = self.run_hook({"transcript_path": f.name})
        finally:
            os.unlink(f.name)
        self.assertEqual(done.returncode, 2)

    def test_garbage_on_stdin_fails_open(self):
        with tempfile.TemporaryDirectory() as home:
            done = subprocess.run([sys.executable, str(HOOK)], input="not json",
                                  capture_output=True, text=True, timeout=20,
                                  env=dict(os.environ, HERALD_HOME=home))
        self.assertEqual(done.returncode, 0)


if __name__ == "__main__":
    unittest.main()
