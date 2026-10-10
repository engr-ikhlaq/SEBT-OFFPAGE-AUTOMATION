"""auto_outreach._html_body - blank entries in the body-lines tuple are
paragraph SEPARATORS, not content to render. They used to also become a
literal <br>, on top of each <p>'s own margin, doubling the visible gap
between paragraphs (reported as "2 line gap between the paragraphs")."""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import auto_outreach


class HtmlBodyTests(unittest.TestCase):
    def test_blank_separators_produce_no_extra_br(self):
        html = auto_outreach._html_body(("Hi,", "", "Second paragraph."))
        self.assertNotIn("<br>", html)

    def test_each_real_line_becomes_its_own_paragraph(self):
        html = auto_outreach._html_body(("Hi,", "", "Second paragraph."))
        self.assertEqual(html.count("<p"), 2)
        self.assertIn("Hi,</p>", html)
        self.assertIn("Second paragraph.</p>", html)

    def test_every_paragraph_gets_one_explicit_margin(self):
        html = auto_outreach._html_body(("A", "", "B", "", "C"))
        self.assertEqual(html.count('margin:0 0 1em;'), 3)

    def test_consecutive_lines_with_no_blank_between_them_stay_separate_paragraphs(self):
        # The bullet list in the real template: three lines in a row, no
        # blank-line separator between them.
        html = auto_outreach._html_body(("- one", "- two", "- three"))
        self.assertEqual(html.count("<p"), 3)


if __name__ == "__main__":
    unittest.main()
