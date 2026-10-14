"""LeadPipeline.run()'s max_leads / on_lead / should_stop, used by the web scrape job."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

from offpage.models import PageData, SearchResult
from offpage.pipeline import _NEGATIVE_SITE_FILTER
from offpage.storage import SeenStore
from offpage.writer import LeadWriter

# Absolute import, not relative (`from .test_pipeline import ...`) - under
# `python -m unittest discover`, this file was sometimes imported without
# offpage_tests package context, so a relative import raised "attempted
# relative import with no known parent package". Matches every other test
# module's sys.path.insert + absolute-import convention in this suite.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pipeline import FakeReader, FakeSearch, FakeSink, _pipeline, _settings


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
            f'pept "q1" {_NEGATIVE_SITE_FILTER}': [SearchResult(url="https://a.test", domain="a.test", snippet="")],
            f'pept "q2" {_NEGATIVE_SITE_FILTER}': [SearchResult(url="https://b.test", domain="b.test", snippet="")],
            f'pept "q3" {_NEGATIVE_SITE_FILTER}': [SearchResult(url="https://c.test", domain="c.test", snippet="")],
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

    def test_should_stop_also_halts_partway_through_one_querys_results(self):
        # A single query can return several results - Stop must take effect
        # between THOSE too, not just once the whole batch is processed
        # (otherwise clicking Stop on a slow page could feel unresponsive).
        settings = _settings(queries=('[KEYWORD] "q1"',))
        by_query = {
            f'pept "q1" {_NEGATIVE_SITE_FILTER}': [
                SearchResult(url="https://a.test", domain="a.test", snippet=""),
                SearchResult(url="https://b.test", domain="b.test", snippet=""),
                SearchResult(url="https://c.test", domain="c.test", snippet=""),
            ],
        }
        reader = FakeReader({
            "a.test": _lead_page("pept"), "b.test": _lead_page("pept"), "c.test": _lead_page("pept"),
        })
        search = FakeSearch(by_query)

        stop_after_first = {"count": 0}

        def should_stop():
            return stop_after_first["count"] >= 1

        with SeenStore(":memory:") as store:
            writer = LeadWriter(FakeSink(), store, batch_size=1)
            pipeline = _pipeline(search, reader, store, writer, settings)

            def on_lead(count, lead):
                stop_after_first["count"] = count

            queued = pipeline.run(on_lead=on_lead, should_stop=should_stop)
        self.assertEqual(queued, 1)  # b.test and c.test never fetched once stop was requested

    def test_no_cap_behaves_as_before(self):
        settings, search, reader = self._three_query_setup()
        with SeenStore(":memory:") as store:
            writer = LeadWriter(FakeSink(), store, batch_size=1)
            pipeline = _pipeline(search, reader, store, writer, settings)
            queued = pipeline.run()
        self.assertEqual(queued, 3)


if __name__ == "__main__":
    unittest.main()
