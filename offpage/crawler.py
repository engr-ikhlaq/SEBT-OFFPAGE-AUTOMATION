"""Plain-HTTP fallback: check common contact pages when a page shows no email."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import requests

from .models import ContactInfo, PageData
from .parsing import PageParser

log = logging.getLogger(__name__)

CONTACT_PATHS: tuple[str, ...] = (
    "/contact",
    "/contact-us",
    "/contact-us/",
    "/contact.html",
    "/about",
    "/team",
    "/editor",
    "/write-for-us",
    "/guest-post",
    "/contributors",
    "/advertise",
    "/index.php/contact",
    "/company/contact",
    "/get-in-touch",
    "/support/contact",
    "/help/contact",
)


class SiteCrawler:
    def __init__(
        self,
        http: requests.Session,
        parser: PageParser,
        timeout: float = 8,
        paths: tuple[str, ...] = CONTACT_PATHS,
        workers: int = 6,
    ) -> None:
        self._http = http
        self._parser = parser
        self._timeout = timeout
        self._paths = paths
        self._workers = max(1, workers)

    def crawl(self, base_url: str) -> ContactInfo:
        """Check the homepage, then every contact path at once.

        Paths are fetched concurrently (they are independent, slow HTTP calls),
        but results are still read in the original priority order, so the
        outcome is the same as fetching them one at a time: the first path
        with an email wins, and the first path with just a contact form is
        the fallback.
        """
        home = self._fetch(base_url)
        home_emails = home.emails if home else frozenset()

        urls = [urljoin(base_url, path) for path in self._paths]
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            pages = pool.map(self._fetch, urls)

        contact_url = ""
        for url, page in zip(urls, pages):
            if page is None:
                continue
            if page.emails:
                return ContactInfo(emails=page.emails, contact_url=url)
            if page.has_form and not contact_url:
                contact_url = url

        if home_emails:
            return ContactInfo(emails=home_emails, contact_url=base_url)
        return ContactInfo(contact_url=contact_url)

    def _fetch(self, url: str) -> PageData | None:
        try:
            response = self._http.get(url, timeout=self._timeout)
        except requests.RequestException as exc:
            log.debug("Fetch failed for %s: %s", url, exc)
            return None
        if response.status_code != 200:
            return None
        return self._parser.parse(response.text)
