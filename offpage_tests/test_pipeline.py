"""End-to-end pipeline behaviour, with every I/O dependency faked."""
from __future__ import annotations

import unittest

from selenium.common.exceptions import WebDriverException

from offpage.authority import AuthoritySignals
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


def _pipeline(search, reader, store, writer, settings=None, **pipeline_kwargs) -> LeadPipeline:
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
        **pipeline_kwargs,
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

    def test_an_on_site_email_is_chosen_over_an_unrelated_one_found_on_the_same_page(self):
        query = 'pept "write for us"'
        results = [SearchResult(url="https://goodsite.com", domain="goodsite.com", snippet="")]
        page = PageData(
            title="Pept write for us",
            body="We accept guest posts about pept. Write for us.",
            emails=frozenset({"editor@goodsite.com", "notify@disqus.com"}),
        )
        search, reader = FakeSearch({query: results}), FakeReader({"goodsite.com": page})

        with SeenStore(":memory:") as store:
            sink = FakeSink()
            writer = LeadWriter(sink, store, batch_size=1)
            queued = _pipeline(search, reader, store, writer).run()
            writer.flush()

            self.assertEqual(queued, 1)
            self.assertEqual(sink.rows[0][5], "editor@goodsite.com")

    def test_a_different_category_page_is_rejected_even_with_guest_post_wording(self):
        # "phone" is a category the taxonomy recognizes (technology) - a
        # food blog that takes guest posts is a confidently different
        # category, so unlike "pept" above, this one IS rejected.
        query = 'phone "write for us"'
        results = [SearchResult(url="https://foodblog.com", domain="foodblog.com", snippet="")]
        page = PageData(
            title="Easy Dinner Recipes",
            body="Our food blog shares cooking recipes and kitchen tips. Write for us guest post.",
            emails=frozenset({"a@foodblog.com"}),
        )
        search, reader = FakeSearch({query: results}), FakeReader({"foodblog.com": page})
        settings = _settings(keywords=("phone",), queries=('[KEYWORD] "write for us"',))
        with SeenStore(":memory:") as store:
            sink = FakeSink()
            writer = LeadWriter(sink, store, batch_size=1)
            queued = _pipeline(search, reader, store, writer, settings).run()
        self.assertEqual(queued, 0)
        self.assertEqual(sink.rows, [])

    def test_an_off_topic_page_with_genuine_guest_post_wording_is_still_captured(self):
        # A travel blog doesn't literally mention "pept", but "pept" isn't
        # a category the taxonomy recognizes either (see
        # offpage/categories.py) - so there's nothing to confidently
        # reject it against, and it's captured as a lead, just with a
        # lower score than an on-topic match would get.
        query = 'pept "write for us"'
        results = [SearchResult(url="https://offtopic.com", domain="offtopic.com", snippet="")]
        page = PageData(title="Travel blog", body="Write for us about travel.", emails=frozenset({"a@offtopic.com"}))
        search, reader = FakeSearch({query: results}), FakeReader({"offtopic.com": page})
        with SeenStore(":memory:") as store:
            sink = FakeSink()
            writer = LeadWriter(sink, store, batch_size=1)
            queued = _pipeline(search, reader, store, writer).run()
            writer.flush()
        self.assertEqual(queued, 1)
        self.assertEqual(sink.rows[0][3], "offtopic.com")


class FakeAuthority:
    """Records every domain it's asked about, in order - lets a test prove
    whether it was consulted at all, and when, relative to the reader."""

    def __init__(self, signals_by_domain: dict[str, AuthoritySignals]):
        self._signals = signals_by_domain
        self.asked_about: list[str] = []

    def lookup(self, domain: str) -> AuthoritySignals:
        self.asked_about.append(domain)
        return self._signals.get(domain, AuthoritySignals(da_score=None, traffic_rank=None))


class RecordingReader:
    """Wraps a FakeReader and records which URLs were actually read - so a
    test can prove a page load was skipped entirely, not just that its
    result was discarded afterward."""

    def __init__(self, inner):
        self._inner = inner
        self.read_urls: list[str] = []

    def read(self, url):
        self.read_urls.append(url)
        return self._inner.read(url)


class PipelineAuthorityOrderingTests(unittest.TestCase):
    """When an actual min_da/max_traffic_rank bar is configured, a low-
    authority domain should be rejected WITHOUT ever loading its page -
    see pipeline.py's _build_lead() for why (no point spending a real
    Selenium page load, and possibly a contact-page crawl on top of that,
    on a domain that was never going to qualify)."""

    def _candidate(self, domain="site.test"):
        query = 'pept "write for us"'
        results = [SearchResult(url=f"https://{domain}", domain=domain, snippet="")]
        page = PageData(
            title="Pept write for us", body="We accept guest posts about pept. Write for us.",
            emails=frozenset({f"a@{domain}"}),
        )
        return query, results, page, domain

    def test_a_domain_below_the_bar_is_rejected_without_reading_its_page(self):
        query, results, page, domain = self._candidate()
        search = FakeSearch({query: results})
        reader = RecordingReader(FakeReader({domain: page}))
        authority = FakeAuthority({domain: AuthoritySignals(da_score=1.0, traffic_rank=None)})

        with SeenStore(":memory:") as store:
            sink = FakeSink()
            writer = LeadWriter(sink, store, batch_size=1)
            pipeline = _pipeline(search, reader, store, writer, authority_checker=authority, min_da=5.0)
            queued = pipeline.run()

            self.assertEqual(queued, 0)
            self.assertEqual(reader.read_urls, [])  # never even tried to load the page
            self.assertEqual(authority.asked_about, [domain])
            self.assertTrue(store.is_seen(domain))  # still remembered, so it's not re-checked next run

    def test_a_domain_meeting_the_bar_proceeds_normally_with_signals_attached(self):
        query, results, page, domain = self._candidate()
        search = FakeSearch({query: results})
        reader = RecordingReader(FakeReader({domain: page}))
        authority = FakeAuthority({domain: AuthoritySignals(da_score=7.5, traffic_rank=1000)})

        with SeenStore(":memory:") as store:
            sink = FakeSink()
            writer = LeadWriter(sink, store, batch_size=1)
            pipeline = _pipeline(search, reader, store, writer, authority_checker=authority, min_da=5.0)
            queued = pipeline.run()
            writer.flush()

        self.assertEqual(queued, 1)
        self.assertEqual(reader.read_urls, [f"https://{domain}"])
        row = sink.rows[0]
        self.assertEqual(row[-2], 7.5)     # DA column
        self.assertEqual(row[-1], 1000)    # Traffic Rank column

    def test_with_no_bar_configured_authority_is_only_checked_after_relevance_passes(self):
        """No min_da/max_traffic_rank set - a domain relevance would
        reject anyway must never trigger an (rate-limited) authority
        lookup at all."""
        query = 'pept "write for us"'
        results = [SearchResult(url="https://offtopic.test", domain="offtopic.test", snippet="")]
        page = PageData(title="Travel blog", body="Nothing relevant here.", emails=frozenset({"a@offtopic.test"}))
        search = FakeSearch({query: results})
        reader = RecordingReader(FakeReader({"offtopic.test": page}))
        authority = FakeAuthority({})

        with SeenStore(":memory:") as store:
            writer = LeadWriter(FakeSink(), store, batch_size=1)
            pipeline = _pipeline(search, reader, store, writer, authority_checker=authority)
            queued = pipeline.run()

        self.assertEqual(queued, 0)
        self.assertEqual(authority.asked_about, [])  # never consulted - relevance rejected it first

    def test_with_no_bar_configured_a_passing_lead_still_gets_signals_attached(self):
        query, results, page, domain = self._candidate()
        search = FakeSearch({query: results})
        reader = RecordingReader(FakeReader({domain: page}))
        authority = FakeAuthority({domain: AuthoritySignals(da_score=3.2, traffic_rank=50000)})

        with SeenStore(":memory:") as store:
            sink = FakeSink()
            writer = LeadWriter(sink, store, batch_size=1)
            pipeline = _pipeline(search, reader, store, writer, authority_checker=authority)
            queued = pipeline.run()
            writer.flush()

        self.assertEqual(queued, 1)
        self.assertEqual(authority.asked_about, [domain])
        row = sink.rows[0]
        self.assertEqual(row[-2], 3.2)
        self.assertEqual(row[-1], 50000)


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
