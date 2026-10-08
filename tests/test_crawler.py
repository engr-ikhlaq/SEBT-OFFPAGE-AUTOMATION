"""SiteCrawler: paths are fetched concurrently but read back in priority order."""
from __future__ import annotations

import time
import unittest

from offpage.crawler import SiteCrawler
from offpage.parsing import PageParser


class FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class FakeHttp:
    """Stands in for requests.Session. Each entry maps a URL fragment to
    (status, html, artificial delay), so tests can make a later path answer
    before an earlier one and check that priority order still wins.
    """

    def __init__(self, pages: dict[str, tuple[int, str, float]]) -> None:
        self._pages = pages
        self.calls: list[str] = []

    def get(self, url: str, timeout: float) -> FakeResponse:
        self.calls.append(url)
        for fragment, (status, html, delay) in self._pages.items():
            if fragment in url:
                time.sleep(delay)
                return FakeResponse(status, html)
        return FakeResponse(404, "")


def _html(has_email: bool = False, has_form: bool = False) -> str:
    body = "<a href='mailto:a@site.test'>mail</a>" if has_email else ""
    body += "<form></form>" if has_form else ""
    return f"<html><body>{body}</body></html>"


class SiteCrawlerOrderTests(unittest.TestCase):
    def test_result_order_follows_paths_not_completion_time(self):
        # "/about" answers fast with just a form; "/contact" answers slowly
        # but has the email. /contact is first in the path list, so it must
        # win even though /about's fetch finishes first.
        http = FakeHttp({
            "/about": (200, _html(has_form=True), 0.0),
            "/contact": (200, _html(has_email=True), 0.08),
        })
        crawler = SiteCrawler(http, PageParser(), paths=("/contact", "/about"), workers=4)

        info = crawler.crawl("https://site.test")

        self.assertEqual(info.emails, frozenset({"a@site.test"}))
        self.assertTrue(info.contact_url.endswith("/contact"))

    def test_falls_back_to_a_contact_form_when_no_path_has_an_email(self):
        http = FakeHttp({
            "/contact": (200, _html(has_form=False), 0.0),
            "/about": (200, _html(has_form=True), 0.0),
        })
        crawler = SiteCrawler(http, PageParser(), paths=("/contact", "/about"), workers=4)

        info = crawler.crawl("https://site.test")

        self.assertEqual(info.emails, frozenset())
        self.assertTrue(info.contact_url.endswith("/about"))

    def test_paths_are_fetched_concurrently(self):
        delay = 0.05
        http = FakeHttp({path: (200, _html(), delay) for path in ("/a", "/b", "/c", "/d")})
        crawler = SiteCrawler(http, PageParser(), paths=("/a", "/b", "/c", "/d"), workers=4)

        started = time.monotonic()
        crawler.crawl("https://site.test")
        elapsed = time.monotonic() - started

        # Sequentially this would take 4 * delay; concurrently it should take about one.
        self.assertLess(elapsed, delay * 2.5)

    def test_workers_is_never_less_than_one(self):
        crawler = SiteCrawler(FakeHttp({}), PageParser(), workers=0)
        self.assertEqual(crawler._workers, 1)


if __name__ == "__main__":
    unittest.main()
