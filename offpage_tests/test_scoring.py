"""Scorer's domain-quality component: static table by default, blended
with real learned reply-rates when a Scorer is given some (see db.py's
get_tld_reply_rates and scrape_job.py's use of it)."""
from __future__ import annotations

import unittest

from offpage.models import PageData
from offpage.scoring import LEARNED_DOMAIN_POINTS_SCALE, Scorer


class DomainPointsTests(unittest.TestCase):
    def test_with_no_learned_rates_the_static_table_is_used(self):
        self.assertEqual(Scorer()._domain_points("example.com"), 5)
        self.assertEqual(Scorer()._domain_points("example.edu"), 15)

    def test_an_unlearned_suffix_still_falls_back_to_static(self):
        scorer = Scorer(tld_reply_rates={".edu": 0.5})
        self.assertEqual(scorer._domain_points("example.com"), 5)  # untouched

    def test_a_learned_rate_overrides_the_static_points_for_that_suffix(self):
        scorer = Scorer(tld_reply_rates={".com": 0.9})
        # 0.9 scaled onto LEARNED_DOMAIN_POINTS_SCALE, not the static "5".
        self.assertEqual(scorer._domain_points("example.com"), round(0.9 * LEARNED_DOMAIN_POINTS_SCALE))

    def test_a_poor_real_reply_rate_can_score_lower_than_the_static_guess(self):
        # .edu LOOKS authoritative (static points=15), but if it's never
        # actually converting in practice, the learned score should say so.
        scorer = Scorer(tld_reply_rates={".edu": 0.05})
        self.assertLess(scorer._domain_points("example.edu"), 15)

    def test_full_score_is_unaffected_when_no_rates_are_given(self):
        # Regression guard: existing callers that construct Scorer() with
        # no arguments must keep getting exactly the old numbers.
        self.assertEqual(Scorer().score("kw", PageData(), "example.com", False, False), 5)


if __name__ == "__main__":
    unittest.main()
