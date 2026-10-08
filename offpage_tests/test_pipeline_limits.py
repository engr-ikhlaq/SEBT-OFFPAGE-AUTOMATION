"""LeadPipeline.run()'s max_leads / on_lead / should_stop, used by the web scrape job."""
from __future__ import annotations

import unittest

from offpage.models import PageData, SearchResult
from offpage.storage import SeenStore
from offpage.writer import LeadWriter

from .test_pipeline import FakeReader, FakeSearch, FakeSink, _pipeline, _settings


def _lead_page(topic: str) -> PageData:
    return PageData(
        title=f"{topic} write for us",
        body=f"We accept guest posts about {topic}. Write for us.",
        emails=frozenset({f"editor@{topic}.test"}),
    )


class MaxLeadsTests(unittest.TestCase):
    def _three_query_setup(self):
        queries = ('[KEYWORD] "q1"', '[KEYWORD] "q2"', '[KEYWORD] "q3"')
        settings = _settings(queries=queries)
        by_query = {
            'pept "q1"': [SearchResult(url="https://a.test", domain="a.test", snippet="")],
            'pept "q2"': [SearchResult(url="https://b.test", domain="b.test", snippet="")],
            'pept "q3"': [SearchResult(url="https://c.test", domain="c.test", snippet="")],
        }
        reader = FakeReader({
            "a.test": _lead_page("pept"), "b.test": _lead_page("pept"), "c.test": _lead_page("pept"),
        })
        search = FakeSearch(by_query)
        return settings, search, reader

    def test_stops_once_the_cap_is_reached(self):
        settings, search, reader = self._three_query_setup()
        with SeenStore(":memory:") as store:
            writer = LeadWriter(FakeSink(), store, batch_size=1)
            pipeline = _pipeline(search, reader, store, writer, settings)
            queued = pipeline.run(max_leads=2)
        self.assertEqual(queued, 2)  # q3's lead is never even fetched

    def test_on_lead_is_called_with_running_count(self):
        settings, search, reader = self._three_query_setup()
        seen_counts = []
        with SeenStore(":memory:") as store:
            writer = LeadWriter(FakeSink(), store, batch_size=1)
            pipeline = _pipeline(search, reader, store, writer, settings)
            pipeline.run(on_lead=lambda count, lead: seen_counts.append((count, lead.domain)))
        self.assertEqual(seen_counts, [(1, "a.test"), (2, "b.test"), (3, "c.test")])

    def test_should_stop_halts_before_the_next_search(self):
        settings, search, reader = self._three_query_setup()
        with SeenStore(":memory:") as store:
            writer = LeadWriter(FakeSink(), store, batch_size=1)
            pipeline = _pipeline(search, reader, store, writer, settings)
            queued = pipeline.run(should_stop=lambda: True)
        self.assertEqual(queued, 0)

    def test_no_cap_behaves_as_before(self):
        settings, search, reader = self._three_query_setup()
        with SeenStore(":memory:") as store:
            writer = LeadWriter(FakeSink(), store, batch_size=1)
            pipeline = _pipeline(search, reader, store, writer, settings)
            queued = pipeline.run()
        self.assertEqual(queued, 3)


if __name__ == "__main__":
    unittest.main()
