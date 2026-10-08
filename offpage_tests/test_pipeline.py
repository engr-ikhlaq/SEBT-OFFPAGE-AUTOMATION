"""End-to-end pipeline behaviour, with every I/O dependency faked."""
from __future__ import annotations

import unittest

from selenium.common.exceptions import WebDriverException

from offpage.config import Settings
from offpage.models import ContactInfo, PageData, SearchResult
from offpage.pipeline import LeadPipeline, SearchFailuresError
from offpage.relevance import RelevanceGate
from offpage.scoring import Scorer
from offpage.storage import SeenStore
from offpage.validation import EmailValidator
from offpage.filters import UrlFilter
from offpage.writer import LeadWriter


class FakeSearch:
    def __init__(self, by_query: dict[str, list[SearchResult]], fail_always: bool = False):
        self._by_query = by_query
        self._fail_always = fail_always

    def run(self, query):
        if self._fail_always:
            raise WebDriverException("no network")
        return self._by_query.get(query, [])


class FakeReader:
    def __init__(self, by_domain: dict[str, PageData]):
        self._by_domain = by_domain

    def read(self, url):
        domain = url.split("//", 1)[1]
        return self._by_domain.get(domain, PageData())


class FakeCrawler:
    def crawl(self, base_url):
        return ContactInfo()


class FakeSink:
    def __init__(self):
        self.rows = []

    def append_rows(self, rows):
        self.rows.extend(rows)


class AcceptAllResolver:
    """Every domain has MX records, so email checks reduce to syntax only."""

    def __init__(self):
        self.lifetime = 5.0

    def resolve(self, domain, rdtype):
        return ["mx.example"]


def _settings(**overrides) -> Settings:
    base = dict(
        spreadsheet_id="x",
        keywords=("pept",),
        queries=('[KEYWORD] "write for us"',),
        max_pages=1,
    )
    base.update(overrides)
    return Settings(**base)


def _pipeline(search, reader, store, writer, settings=None) -> LeadPipeline:
    scorer = Scorer()
    return LeadPipeline(
        settings=settings or _settings(),
        search=search,
        reader=reader,
        crawler=FakeCrawler(),
        scorer=scorer,
        gate=RelevanceGate(scorer),
        email_validator=EmailValidator(resolver=AcceptAllResolver()),
        url_filter=UrlFilter(),
        store=store,
        writer=writer,
    )


class PipelineEmailRuleTests(unittest.TestCase):
    def test_a_relevant_page_without_a_verified_email_is_not_uploaded(self):
        query = 'pept "write for us"'
        results = {query: [SearchResult(url="https://goodsite.com", domain="goodsite.com", snippet="")]}
        page = PageData(title="Pept write for us", body="We accept guest posts about pept. Write for us.")
        search, reader = FakeSearch({query: results[query]}), FakeReader({"goodsite.com": page})

        with SeenStore(":memory:") as store:
            sink = FakeSink()
            writer = LeadWriter(sink, store, batch_size=1)
            queued = _pipeline(search, reader, store, writer).run()
            writer.flush()

            self.assertEqual(queued, 0)
            self.assertEqual(sink.rows, [])
            # Rejected for having no email, and remembered so it is not retried.
            self.assertTrue(store.is_seen("goodsite.com"))

    def test_a_relevant_page_with_a_verified_email_is_uploaded(self):
        query = 'pept "write for us"'
        results = [SearchResult(url="https://goodsite.com", domain="goodsite.com", snippet="")]
        page = PageData(
            title="Pept write for us",
            body="We accept guest posts about pept. Write for us.",
            emails=frozenset({"editor@goodsite.com"}),
        )
        search, reader = FakeSearch({query: results}), FakeReader({"goodsite.com": page})

        with SeenStore(":memory:") as store:
            sink = FakeSink()
            writer = LeadWriter(sink, store, batch_size=1)
            queued = _pipeline(search, reader, store, writer).run()
            writer.flush()

            self.assertEqual(queued, 1)
            self.assertEqual(sink.rows[0][3], "goodsite.com")
            self.assertEqual(sink.rows[0][5], "editor@goodsite.com")

    def test_relevance_is_checked_against_the_keyword_not_the_full_query(self):
        # The query text contains extra words ("write for us") that are not part
        # of the keyword and must not be required on the page.
        query = 'pept "write for us"'
        results = [SearchResult(url="https://goodsite.com", domain="goodsite.com", snippet="")]
        page = PageData(
            title="Pept basics",
            body="Pept guest post: write for us.",
            emails=frozenset({"a@goodsite.com"}),
        )
        search, reader = FakeSearch({query: results}), FakeReader({"goodsite.com": page})
        with SeenStore(":memory:") as store:
            writer = LeadWriter(FakeSink(), store, batch_size=1)
            queued = _pipeline(search, reader, store, writer).run()
        self.assertEqual(queued, 1)

    def test_off_topic_relevant_only_to_query_words_is_rejected(self):
        # Page matches words from the query ("write for us") but not the keyword itself.
        query = 'pept "write for us"'
        results = [SearchResult(url="https://offtopic.com", domain="offtopic.com", snippet="")]
        page = PageData(title="Travel blog", body="Write for us about travel.", emails=frozenset({"a@offtopic.com"}))
        search, reader = FakeSearch({query: results}), FakeReader({"offtopic.com": page})
        with SeenStore(":memory:") as store:
            sink = FakeSink()
            writer = LeadWriter(sink, store, batch_size=1)
            queued = _pipeline(search, reader, store, writer).run()
        self.assertEqual(queued, 0)
        self.assertEqual(sink.rows, [])


class PipelineFailureStopTests(unittest.TestCase):
    def test_stops_after_repeated_search_failures(self):
        settings = _settings(queries=tuple(f'[KEYWORD] "q{i}"' for i in range(10)))
        search = FakeSearch({}, fail_always=True)
        with SeenStore(":memory:") as store:
            pipeline = _pipeline(search, FakeReader({}), store, LeadWriter(FakeSink(), store, 1), settings)
            with self.assertRaises(SearchFailuresError):
                pipeline.run()


if __name__ == "__main__":
    unittest.main()
