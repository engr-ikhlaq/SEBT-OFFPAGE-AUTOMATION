"""Orchestrates one run: search, filter, read, check relevance, verify emails, score, record."""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from datetime import date
from typing import Protocol

from selenium.common.exceptions import InvalidSessionIdException, WebDriverException

from .config import Settings
from .filters import UrlFilter
from .models import ContactInfo, Lead, PageData, SearchResult
from .relevance import RelevanceGate
from .scoring import Scorer
from .validation import EmailValidator, is_plausible_domain
from .writer import LeadWriter

log = logging.getLogger(__name__)

RUN_DATE_FORMAT = "%d-%b-%Y"
MAX_CONSECUTIVE_SEARCH_FAILURES = 5


class SearchFailuresError(RuntimeError):
    """Searches keep failing in a row, usually because the network is down."""


class Searcher(Protocol):
    def run(self, query: str) -> list[SearchResult]: ...


class Reader(Protocol):
    def read(self, url: str) -> PageData: ...


class Crawler(Protocol):
    def crawl(self, base_url: str) -> ContactInfo: ...


class SeenStore(Protocol):
    def is_seen(self, domain: str) -> bool: ...
    def mark_seen(self, domains: Iterable[str]) -> None: ...


class LeadPipeline:
    def __init__(
        self,
        *,
        settings: Settings,
        search: Searcher,
        reader: Reader,
        crawler: Crawler,
        scorer: Scorer,
        gate: RelevanceGate,
        email_validator: EmailValidator,
        url_filter: UrlFilter,
        store: SeenStore,
        writer: LeadWriter,
    ) -> None:
        self._settings = settings
        self._search = search
        self._reader = reader
        self._crawler = crawler
        self._scorer = scorer
        self._gate = gate
        self._emails = email_validator
        self._filter = url_filter
        self._store = store
        self._writer = writer

    def run(
        self,
        max_leads: int | None = None,
        on_lead: Callable[[int, Lead], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> int:
        """Process every keyword and query. Returns the number of leads queued.

        max_leads: stop (returning early) once this many leads have been
            queued, instead of working through every keyword/query. The
            caller can run() again later — already-seen domains are
            skipped automatically, so a second call picks up where the
            first left off.
        on_lead: called as on_lead(queued_so_far, lead) right after each
            lead is queued — lets a caller (e.g. a background job) report
            live progress without waiting for run() to return.
        should_stop: polled between searches; if it returns True, run()
            stops as cleanly as hitting max_leads. For a caller that needs
            to cancel a long run (e.g. the user closed the page).
        """
        queued = 0
        failures_in_row = 0
        run_domains: set[str] = set()

        for keyword in self._settings.keywords:
            for template in self._settings.queries:
                if should_stop is not None and should_stop():
                    return queued

                query = template.replace("[KEYWORD]", keyword)
                log.info("Search: %s", query)
                try:
                    results = self._search.run(query)
                except InvalidSessionIdException:
                    # The browser is gone; every later search would fail too.
                    raise
                except WebDriverException:
                    failures_in_row += 1
                    log.exception("Search failed, skipping: %s", query)
                    if failures_in_row >= MAX_CONSECUTIVE_SEARCH_FAILURES:
                        raise SearchFailuresError(
                            f"{failures_in_row} searches failed in a row. "
                            "Check the network and restart main.py."
                        )
                    continue
                failures_in_row = 0

                for result in self._candidates(results):
                    # Checked here too, not just between queries - a single
                    # query's results can take a while to read/crawl/verify,
                    # and Stop should take effect promptly rather than only
                    # after the whole batch of results finishes. Whatever
                    # was already queued stays queued (LeadWriter has
                    # already saved it, batch_size=1 by default).
                    if should_stop is not None and should_stop():
                        return queued

                    # run_domains covers leads still buffered (not yet marked seen in the store).
                    if result.domain in run_domains or self._store.is_seen(result.domain):
                        continue
                    run_domains.add(result.domain)
                    lead = self._build_lead(keyword, query, result)
                    if lead is None:
                        continue
                    self._writer.add(lead)
                    queued += 1
                    if on_lead is not None:
                        on_lead(queued, lead)
                    if max_leads is not None and queued >= max_leads:
                        return queued
        return queued

    def _candidates(self, results: Iterable[SearchResult]) -> Iterator[SearchResult]:
        seen: set[str] = set()
        for result in results:
            if result.domain in seen or not is_plausible_domain(result.domain):
                continue
            if not self._filter.allows(result.url):
                continue
            seen.add(result.domain)
            yield result

    def _skip(self, domain: str, reason: str, *, level: int = logging.INFO) -> None:
        """Log why a domain was rejected and remember it, so it is not visited again."""
        log.log(level, "Skipped %s: %s", domain, reason)
        self._store.mark_seen([domain])

    def _build_lead(self, keyword: str, query: str, result: SearchResult) -> Lead | None:
        try:
            page = self._reader.read(result.url)
        except InvalidSessionIdException:
            raise
        except WebDriverException:
            self._skip(result.domain, "could not render the page", level=logging.WARNING)
            return None

        reason = self._gate.rejection_reason(keyword, page)
        if reason:
            self._skip(result.domain, f"not relevant ({reason})")
            return None

        emails = set(page.emails)
        contact_url = ""
        if not emails:
            info = self._crawler.crawl(result.url)
            emails, contact_url = set(info.emails), info.contact_url

        verified = self._emails.filter_valid(emails)
        if not verified:
            self._skip(result.domain, "no verified email")
            return None

        score = self._scorer.score(keyword, page, result.domain, True, bool(contact_url))
        log.info("Scored %s: %d (%d verified email(s))", result.domain, score, len(verified))

        return Lead(
            run_date=date.today().strftime(RUN_DATE_FORMAT),
            keyword=keyword,
            query=query,
            domain=result.domain,
            url=result.url,
            emails=tuple(verified),
            contact_url=contact_url,
            snippet=result.snippet,
            score=score,
        )
