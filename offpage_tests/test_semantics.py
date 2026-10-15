"""Calibration data for offpage.semantics.similarity() and the
TOPIC_SIMILARITY_FLOOR it's compared against in offpage/relevance.py.

These numbers come from actually running the real en_core_web_md model
against hand-picked phrase pairs (not a mock) - skipped automatically if
the model isn't installed, since that's a real, if unlikely, setup gap
rather than a bug to fail the whole suite over.
"""
from __future__ import annotations

import unittest

from offpage import semantics
from offpage.relevance import TOPIC_SIMILARITY_FLOOR

try:
    _MODEL_AVAILABLE = semantics._model() is not None
except Exception:
    _MODEL_AVAILABLE = False


@unittest.skipUnless(_MODEL_AVAILABLE, "en_core_web_md is not installed")
class SemanticSimilarityTests(unittest.TestCase):
    def test_clearly_different_subjects_fall_below_the_floor(self):
        cases = [
            ("phone", "Our food blog shares cooking recipes and kitchen tips."),
            ("phone", "Our hospital blog covers heart surgery, patient recovery and treatment."),
            ("heart surgery", "We review laptops, smartphones and software on our tech blog."),
        ]
        for keyword, text in cases:
            sim = semantics.similarity(keyword, text)
            self.assertIsNotNone(sim, f"{keyword!r} vs {text!r}")
            self.assertLess(sim, TOPIC_SIMILARITY_FLOOR, f"{keyword!r} vs {text!r} -> {sim}")

    def test_the_same_or_an_adjacent_subject_clears_the_floor(self):
        cases = [
            ("phone", "We review the latest smartphones, apps and gadgets."),
            ("sim registration", "We cover telecom carriers, 5G rollouts and wireless networks."),
            ("heart surgery", "Our hospital covers cardiac treatment, surgery and patient recovery plans."),
            ("snapchat cloaking agency", "We help advertisers run safe ad campaigns across social platforms."),
        ]
        for keyword, text in cases:
            sim = semantics.similarity(keyword, text)
            self.assertIsNotNone(sim, f"{keyword!r} vs {text!r}")
            self.assertGreaterEqual(sim, TOPIC_SIMILARITY_FLOOR, f"{keyword!r} vs {text!r} -> {sim}")

    def test_a_keyword_with_no_recognizable_words_returns_none(self):
        self.assertIsNone(semantics.similarity("pept", "Write for us if you love travel guest posts."))


if __name__ == "__main__":
    unittest.main()
