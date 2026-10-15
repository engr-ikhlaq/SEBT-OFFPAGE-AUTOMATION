"""offpage.guidelines - lifting a site's own stated word-count ask
straight off its page, for honest per-lead pitch personalization."""
from __future__ import annotations

import unittest

from offpage.guidelines import extract_word_count_requirement


class WordCountExtractionTests(unittest.TestCase):
    def test_a_range_is_extracted(self):
        self.assertEqual(
            extract_word_count_requirement("Posts should be 500-1000 words in length."),
            "500-1000 words",
        )

    def test_a_minimum_phrasing_is_extracted(self):
        self.assertEqual(
            extract_word_count_requirement("We require a minimum of 800 words per submission."),
            "800+ words",
        )

    def test_an_at_least_phrasing_is_extracted(self):
        self.assertEqual(
            extract_word_count_requirement("Articles must be at least 600 words."),
            "600+ words",
        )

    def test_a_plus_sign_is_extracted(self):
        self.assertEqual(
            extract_word_count_requirement("We're looking for 1000+ word articles."),
            "1000+ words",
        )

    def test_a_page_that_never_mentions_word_count_returns_none(self):
        self.assertIsNone(extract_word_count_requirement("We welcome guest posts. Write for us."))

    def test_empty_text_returns_none(self):
        self.assertIsNone(extract_word_count_requirement(""))

    def test_an_unrelated_number_near_the_word_words_is_not_a_false_positive(self):
        # "10 words" in a sentence about something else entirely shouldn't
        # be mistaken for a submission requirement - acceptable to match
        # literally (this function only extracts, callers judge context),
        # but it must not crash or return garbage.
        result = extract_word_count_requirement("Choose your 5 favorite words to describe our brand.")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
