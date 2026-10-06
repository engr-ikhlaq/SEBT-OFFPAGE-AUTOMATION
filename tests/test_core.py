"""Unit tests for the pure logic: filtering, parsing, scoring, storage and writing."""
from __future__ import annotations

import unittest

from offpage.filters import UrlFilter, domain_of
from offpage.models import Lead, PageData
from offpage.parsing import PageParser, extract_emails
from offpage.scoring import Scorer
from offpage.storage import SeenStore
from offpage.writer import LeadWriter


def _lead(domain: str, emails: tuple[str, ...] = ("a@b.com",)) -> Lead:
    return Lead(
        run_date="01-Jan-2026",
        keyword="kw",
        query="q",
        domain=domain,
        url=f"https://{domain}/",
        emails=emails,
        contact_url="",
        snippet="snippet",
        score=10,
    )


class UrlFilterTests(unittest.TestCase):
    def test_blocks_social_and_builder_hosts_and_subdomains(self):
        f = UrlFilter()
        self.assertFalse(f.allows("https://www.facebook.com/page"))
        self.assertFalse(f.allows("https://myblog.wordpress.com/post"))

    def test_does_not_block_substring_lookalikes(self):
        f = UrlFilter()
        self.assertTrue(f.allows("https://fox.com/write-for-us"))
        self.assertTrue(f.allows("https://administration.edu/write-for-us"))

    def test_blocks_bad_paths_and_file_types(self):
        f = UrlFilter()
        self.assertFalse(f.allows("https://example.com/login"))
        self.assertFalse(f.allows("https://example.com/report.pdf"))

    def test_allows_app_tld_now_that_matching_is_by_host(self):
        self.assertTrue(UrlFilter().allows("https://example.app/write-for-us"))

    def test_rejects_non_http_schemes(self):
        self.assertFalse(UrlFilter().allows("ftp://example.com/file"))

    def test_allowed_suffixes_restrict_hosts(self):
        f = UrlFilter([".edu", ".org"])
        self.assertTrue(f.allows("https://cs.stanford.edu/research"))
        self.assertFalse(f.allows("https://example.com/research"))

    def test_domain_of_strips_www(self):
        self.assertEqual(domain_of("https://www.example.co.uk/a"), "example.co.uk")


class EmailTests(unittest.TestCase):
    def test_normalises_case_and_drops_asset_names(self):
        self.assertEqual(
            extract_emails("Mail Info@Example.com logo@2x.png"),
            frozenset({"info@example.com"}),
        )


class PageParserTests(unittest.TestCase):
    def test_extracts_fields_and_ignores_script_text(self):
        html = """
        <html><head>
          <title>Write for Us - Blog</title>
          <meta name="description" content="We accept guest posts">
        </head><body>
          <script>var x = "hidden@x.com";</script>
          <h1>Guest Posts</h1>
          <form></form>
          <a href="mailto:editor@blog.com?subject=hi">mail</a>
          <p>Sales: sales@blog.com</p>
        </body></html>
        """
        page = PageParser().parse(html)
        self.assertEqual(page.title, "Write for Us - Blog")
        self.assertEqual(page.h1, "Guest Posts")
        self.assertEqual(page.meta, "We accept guest posts")
        self.assertTrue(page.has_form)
        self.assertEqual(page.emails, frozenset({"editor@blog.com", "sales@blog.com"}))


class ScorerTests(unittest.TestCase):
    def test_empty_page_only_gets_domain_points(self):
        self.assertEqual(Scorer().score("kw", PageData(), "example.com", False, False), 5)

    def test_score_is_capped_at_100(self):
        page = PageData(
            title="write for us blog post today",
            h1="write for us",
            meta="write for us, guest post and sponsored post details",
            body="write for us guest post submit article pitch us sponsored post " * 50,
            emails=frozenset({"a@b.edu"}),
            has_form=True,
        )
        score = Scorer().score("write for us", page, "b.edu", True, True)
        self.assertLessEqual(score, 100)
        self.assertGreater(score, 0)

    def test_intent_points_are_capped(self):
        body = "write for us guest post submit article pitch us sponsored post contributor guidelines"
        self.assertLessEqual(Scorer().intent_points(body), 25)


class SeenStoreTests(unittest.TestCase):
    def test_mark_seen_round_trip(self):
        with SeenStore(":memory:") as store:
            self.assertFalse(store.is_seen("a.com"))
            store.mark_seen(["a.com", "a.com"])
            self.assertTrue(store.is_seen("a.com"))


class FakeSink:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.rows: list[list] = []

    def append_rows(self, rows):
        if self.fail:
            raise OSError("simulated Sheets failure")
        self.rows.extend(rows)


class FakeStore:
    def __init__(self) -> None:
        self.seen: set[str] = set()

    def mark_seen(self, domains):
        self.seen.update(domains)


class LeadWriterTests(unittest.TestCase):
    def test_domains_marked_seen_only_after_successful_write(self):
        sink, store = FakeSink(fail=True), FakeStore()
        writer = LeadWriter(sink, store, batch_size=10)
        writer.add(_lead("a.com"))

        with self.assertRaises(OSError):
            writer.flush()
        self.assertEqual(store.seen, set())
        self.assertEqual(writer.pending, 1)

        sink.fail = False
        self.assertEqual(writer.flush(), 1)
        self.assertEqual(store.seen, {"a.com"})
        self.assertEqual(writer.pending, 0)
        self.assertEqual(len(sink.rows), 1)

    def test_batches_flush_when_full(self):
        sink, store = FakeSink(), FakeStore()
        writer = LeadWriter(sink, store, batch_size=2)
        for domain in ("a.com", "b.com", "c.com"):
            writer.add(_lead(domain))
        self.assertEqual(len(sink.rows), 2)
        self.assertEqual(writer.pending, 1)

    def test_rejects_invalid_batch_size(self):
        with self.assertRaises(ValueError):
            LeadWriter(FakeSink(), FakeStore(), batch_size=0)


class LeadRowTests(unittest.TestCase):
    def test_row_is_single_line_and_capped(self):
        lead = Lead(
            run_date="01-Jan-2026",
            keyword="kw",
            query="q",
            domain="a.com",
            url="https://a.com/",
            emails=("a@b.com", "c@d.com"),
            contact_url="",
            snippet="line one\nline two " + "x" * 1000,
            score=7,
        )
        row = lead.to_row()
        self.assertNotIn("\n", row[7])
        self.assertLessEqual(len(row[7]), 500)
        self.assertEqual(row[5], "a@b.com, c@d.com")
        self.assertEqual(row[8], 7)


if __name__ == "__main__":
    unittest.main()
