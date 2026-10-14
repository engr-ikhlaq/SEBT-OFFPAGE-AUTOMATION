"""offpage.authority.AuthorityChecker - DA (Open PageRank) and traffic
rank (Tranco) lookups, with HTTP faked so this never hits the real APIs."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from offpage.authority import AuthorityChecker, AuthoritySignals


class FakeResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json = json_data or {}
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code != 429:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    """Routes GET calls by URL prefix to canned responses; records every call."""
    def __init__(self, responses: dict[str, FakeResponse]):
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params or {}))
        for prefix, resp in self._responses.items():
            if url.startswith(prefix):
                return resp
        return FakeResponse(status_code=404)


class OpenPageRankTests(unittest.TestCase):
    def test_no_key_configured_returns_none_without_any_http_call(self):
        http = FakeHttp({})
        checker = AuthorityChecker(open_pagerank_key=None, http=http)
        signals = checker.lookup("example.com")
        self.assertIsNone(signals.da_score)
        self.assertFalse(any("openpagerank" in url for url, _ in http.calls))

    def test_a_valid_response_is_parsed(self):
        http = FakeHttp({
            "https://openpagerank.com": FakeResponse({
                "response": [{"status_code": 200, "page_rank_decimal": 7.23}],
            }),
            "https://tranco-list.eu": FakeResponse({"ranks": []}),
        })
        checker = AuthorityChecker(open_pagerank_key="fake-key", http=http)
        signals = checker.lookup("example.com")
        self.assertEqual(signals.da_score, 7.23)

    def test_a_domain_not_found_by_open_pagerank_is_none(self):
        http = FakeHttp({
            "https://openpagerank.com": FakeResponse({
                "response": [{"status_code": 404, "error": "not found"}],
            }),
            "https://tranco-list.eu": FakeResponse({"ranks": []}),
        })
        checker = AuthorityChecker(open_pagerank_key="fake-key", http=http)
        self.assertIsNone(checker.lookup("example.com").da_score)


class TrancoTests(unittest.TestCase):
    def test_takes_the_most_recent_entry_not_the_oldest(self):
        """Verified against the real API: entries come back newest-first."""
        http = FakeHttp({
            "https://tranco-list.eu": FakeResponse({
                "ranks": [
                    {"date": "2026-10-09", "rank": 42},
                    {"date": "2026-10-08", "rank": 50},
                    {"date": "2026-10-07", "rank": 55},
                ],
            }),
        })
        checker = AuthorityChecker(http=http)
        self.assertEqual(checker.lookup("example.com").traffic_rank, 42)

    def test_a_domain_not_in_the_top_10m_has_no_rank(self):
        http = FakeHttp({"https://tranco-list.eu": FakeResponse({"ranks": []})})
        checker = AuthorityChecker(http=http)
        self.assertIsNone(checker.lookup("tiny-unranked-site.test").traffic_rank)

    def test_a_rate_limit_is_retried_then_gives_up_gracefully(self):
        calls = {"n": 0}

        class RateLimitedHttp:
            def get(self, url, params=None, headers=None, timeout=None):
                calls["n"] += 1
                return FakeResponse(status_code=429)

        checker = AuthorityChecker(http=RateLimitedHttp())
        with mock.patch("offpage.authority.time.sleep"):  # don't actually wait out the backoff in tests
            signals = checker.lookup("example.com")
        self.assertIsNone(signals.traffic_rank)  # degrades to "unknown", never raises
        self.assertGreater(calls["n"], 1)  # actually retried, not just one attempt

    def test_a_network_error_degrades_to_none_instead_of_raising(self):
        class BrokenHttp:
            def get(self, *a, **kw):
                raise ConnectionError("no network")

        checker = AuthorityChecker(http=BrokenHttp())
        signals = checker.lookup("example.com")
        self.assertIsNone(signals.traffic_rank)


class CachingTests(unittest.TestCase):
    def test_a_second_lookup_for_the_same_domain_makes_no_new_http_calls(self):
        http = FakeHttp({"https://tranco-list.eu": FakeResponse({"ranks": [{"date": "d", "rank": 1}]})})
        checker = AuthorityChecker(http=http)
        checker.lookup("example.com")
        calls_after_first = len(http.calls)
        checker.lookup("example.com")
        self.assertEqual(len(http.calls), calls_after_first)

    def test_different_domains_are_looked_up_independently(self):
        http = FakeHttp({"https://tranco-list.eu": FakeResponse({"ranks": [{"date": "d", "rank": 1}]})})
        checker = AuthorityChecker(http=http)
        checker.lookup("a.test")
        checker.lookup("b.test")
        self.assertEqual(len(http.calls), 2)


class MeetsThresholdTests(unittest.TestCase):
    def test_no_thresholds_configured_always_passes(self):
        signals = AuthoritySignals(da_score=1.0, traffic_rank=9_000_000)
        self.assertTrue(signals.meets(min_da=0, max_rank=None))

    def test_below_minimum_da_is_rejected(self):
        signals = AuthoritySignals(da_score=2.0, traffic_rank=None)
        self.assertFalse(signals.meets(min_da=5.0, max_rank=None))

    def test_at_or_above_minimum_da_passes(self):
        signals = AuthoritySignals(da_score=5.0, traffic_rank=None)
        self.assertTrue(signals.meets(min_da=5.0, max_rank=None))

    def test_above_max_rank_is_rejected(self):
        signals = AuthoritySignals(da_score=None, traffic_rank=2_000_000)
        self.assertFalse(signals.meets(min_da=0, max_rank=500_000))

    def test_missing_signals_never_block_on_their_own(self):
        """An unranked/unmeasured site isn't necessarily low-quality - a
        strict min_da/max_rank must not reject what it couldn't measure."""
        signals = AuthoritySignals(da_score=None, traffic_rank=None)
        self.assertTrue(signals.meets(min_da=5.0, max_rank=500_000))


if __name__ == "__main__":
    unittest.main()
