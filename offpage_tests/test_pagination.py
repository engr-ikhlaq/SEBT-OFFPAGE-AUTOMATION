"""GoogleSearch.run()'s pagination control flow - stops as soon as a page
comes back empty instead of working through the rest of max_pages, since
clicking "next" past a query's real results risks looking more automated
to Google, not less (see the fix for a real "did not match any documents"
page turning up at page 2-4 of a narrow query once SCRAPE_MAX_PAGES was
raised to 5). Selenium internals (driver, WebDriverWait, the search box)
are mocked out - this tests the loop's decisions, not the browser calls."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from offpage.browser import GoogleSearch
from offpage.models import SearchResult


def _make_search(pages_of_results: list[list[SearchResult]], max_pages: int = 5) -> GoogleSearch:
    """A GoogleSearch whose _parse_results() returns pages_of_results[0],
    [1], ... on successive calls (an empty list once the list runs out),
    with every other Selenium-touching step stubbed to a no-op so only
    run()'s own pagination logic is under test."""
    session = mock.MagicMock()
    session.driver = mock.MagicMock()
    gs = GoogleSearch(session, max_pages=max_pages)
    gs._wait_for_captcha = mock.Mock()
    gs._go_next = mock.Mock(return_value=True)

    call_count = {"n": 0}

    def fake_parse_results():
        i = call_count["n"]
        call_count["n"] += 1
        return pages_of_results[i] if i < len(pages_of_results) else []

    gs._parse_results = mock.Mock(side_effect=fake_parse_results)
    return gs, session


class EarlyStopOnEmptyPageTests(unittest.TestCase):
    def _run(self, gs):
        with mock.patch("offpage.browser.WebDriverWait") as wait_cls, \
             mock.patch("offpage.browser._type_slowly"), \
             mock.patch("offpage.browser._pause"):
            wait_cls.return_value.until.return_value = mock.MagicMock()
            return gs.run("some query")

    def test_stops_as_soon_as_a_page_is_empty_instead_of_using_the_full_budget(self):
        page1 = [SearchResult(url="https://a.test", domain="a.test", snippet="")]
        gs, _ = _make_search([page1, []], max_pages=5)
        results = self._run(gs)
        self.assertEqual(results, page1)
        self.assertEqual(gs._parse_results.call_count, 2)  # page 1, then the now-empty page 2 - no further
        # _go_next() fires once to reach page 2 at all - but not a second
        # time to go looking for a page 3, once page 2 turns up empty.
        self.assertEqual(gs._go_next.call_count, 1)

    def test_a_query_with_results_on_every_page_uses_the_full_budget(self):
        pages = [[SearchResult(url=f"https://p{i}.test", domain=f"p{i}.test", snippet="")] for i in range(5)]
        gs, _ = _make_search(pages, max_pages=5)
        results = self._run(gs)
        self.assertEqual(len(results), 5)
        self.assertEqual(gs._parse_results.call_count, 5)
        self.assertEqual(gs._go_next.call_count, 4)  # clicked "next" between each of the 5 pages, not after the last

    def test_the_very_first_page_being_empty_returns_an_empty_list_cleanly(self):
        gs, _ = _make_search([[]], max_pages=5)
        results = self._run(gs)
        self.assertEqual(results, [])
        self.assertEqual(gs._parse_results.call_count, 1)
        gs._go_next.assert_not_called()

    def test_go_next_returning_false_still_stops_normally_as_before(self):
        """Unrelated to the empty-page fix - Google simply has no further
        pages (no "next" button) - already-existing behavior, confirmed
        still intact."""
        page1 = [SearchResult(url="https://a.test", domain="a.test", snippet="")]
        gs, _ = _make_search([page1, page1], max_pages=5)
        gs._go_next = mock.Mock(return_value=False)
        results = self._run(gs)
        self.assertEqual(len(results), 1)  # stopped after page 1 since _go_next() said there's no page 2
        self.assertEqual(gs._parse_results.call_count, 1)


if __name__ == "__main__":
    unittest.main()
