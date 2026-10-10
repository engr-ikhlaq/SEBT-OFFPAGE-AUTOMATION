"""auto_outreach._build_outreach_message - sending the exact same template
to every recipient is itself a spam signal (near-duplicate bulk content),
separately from what the content says. Each call recombines independent
subject/opening/pitch/closer/signoff variants, so different leads get
different-looking (but equally legitimate) pitches."""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import auto_outreach


class BuildOutreachMessageTests(unittest.TestCase):
    def test_returns_a_subject_and_nonempty_body_lines(self):
        subject, lines = auto_outreach._build_outreach_message()
        self.assertTrue(subject)
        self.assertTrue(lines)

    def test_repeated_calls_are_not_all_identical(self):
        results = {auto_outreach._build_outreach_message() for _ in range(40)}
        self.assertGreater(len(results), 1)

    def test_every_variant_combination_keeps_the_placeholders_intact(self):
        # Whichever variants get picked, the substitution points mailer.py
        # relies on must still be present somewhere in the message.
        for _ in range(40):
            subject, lines = auto_outreach._build_outreach_message()
            body = "\n".join(lines)
            self.assertIn("{{domain}}", subject)
            self.assertIn("{{url}}", body)
            self.assertIn("{{keyword}}", body)
            self.assertIn("{{sender_name}}", body)

    def test_bullet_list_is_always_present_and_unchanged(self):
        _, lines = auto_outreach._build_outreach_message()
        for bullet in auto_outreach._BULLET_LINES:
            self.assertIn(bullet, lines)

    def test_renders_cleanly_through_html_and_text_body(self):
        subject, lines = auto_outreach._build_outreach_message()
        html = auto_outreach._html_body(lines)
        text = auto_outreach._text_body(lines)
        self.assertIn("<p", html)
        self.assertIn("{{sender_name}}", text)


if __name__ == "__main__":
    unittest.main()
