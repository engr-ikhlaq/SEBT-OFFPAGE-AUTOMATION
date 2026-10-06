"""Orchestrates one run: search, filter, read, score, record."""
from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from datetime import date
from typing import Protocol

from selenium.common.exceptions import WebDriverException

from .config import Settings
from .filters import UrlFilter
from .models import ContactInfo, Lead, PageData, SearchResult
from .scoring import Scorer
from .writer import LeadWriter

log = logging.getLogger(__name__)

RUN_DATE_FORMAT = "%d-%b-%Y"


class Searcher(Protocol):
    def run(self, query: str) -> list[SearchResult]: ...


class Reader(Protocol):
    def read(self, url: str) -> PageData: ...


class Crawler(Protocol):
    def crawl(self, base_url: str) -> ContactInfo: ...


class SeenChecker(Protocol):
    def is_seen(self, domain: str) -> bool: ...


class LeadPipeline:
    def __init__(
        self,
        *,
        settings: Settings,
        search: Searcher,
        reader: Reader,
        crawler: Crawler,
        scorer: Scorer,
        url_filter: UrlFilter,
        store: SeenChecker,
        writer: LeadWriter,
    ) -> None:
        self._settings = settings
        self._search = search
        self._reader = reader
        self._crawler = crawler
        self._scorer = scorer
        self._filter = url_filter
        self._store = store
        self._writer = writer

    def run(self) -> int:
        """Process every keyword and query. Returns the number of leads queued."""
        queued = 0
        run_domains: set[str] = set()

        for keyword in self._settings.keywords:
            for template in self._settings.queries:
                query = template.replace("[KEYWORD]", keyword)
                log.info("Search: %s", query)
                try:
                    results = self._search.run(query)
                except WebDriverException:
                    log.exception("Search failed, skipping: %s", query)
                    continue

                for result in self._candidates(results):
                    # run_domains covers leads still buffered (not yet marked seen in the store).
                    if result.domain in run_domains or self._store.is_seen(result.domain):
                        continue
                    run_domains.add(result.domain)
                    self._writer.add(self._build_lead(keyword, query, result))
                    queued += 1
        return queued

    def _candidates(self, results: Iterable[SearchResult]) -> Iterator[SearchResult]:
        seen: set[str] = set()
        for result in results:
            if result.domain in seen or not self._filter.allows(result.url):
                continue
            seen.add(result.domain)
            yield result

    def _build_lead(self, keyword: str, query: str, result: SearchResult) -> Lead:
        try:
            page = self._reader.read(result.url)
        except WebDriverException:
            log.warning("Could not render %s; using HTTP fallback only", result.url)
            page = PageData()

        emails = set(page.emails)
        contact_url = ""
        if not emails:
            info = self._crawler.crawl(result.url)
            emails, contact_url = set(info.emails), info.contact_url

        score = self._scorer.score(
            keyword, page, result.domain, bool(emails), bool(contact_url)
        )
        log.info("Scored %s: %d (%d email(s))", result.domain, score, len(emails))

        return Lead(
            run_date=date.today().strftime(RUN_DATE_FORMAT),
            keyword=keyword,
            query=query,
            domain=result.domain,
            url=result.url,
            emails=tuple(sorted(emails)),
            contact_url=contact_url,
            snippet=result.snippet,
            score=score,
        )
