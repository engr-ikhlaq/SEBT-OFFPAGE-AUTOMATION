"""Chrome-driven steps: Google search and page rendering."""
from __future__ import annotations

import logging
import random
import time
from types import TracebackType
from typing import Protocol

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .filters import domain_of
from .models import PageData, SearchResult
from .validation import is_plausible_domain

log = logging.getLogger(__name__)


class SearchBlockedError(RuntimeError):
    """Google is showing a CAPTCHA or block page. Stop and retry later."""


def _pause(low: float, high: float) -> None:
    time.sleep(random.uniform(low, high))


class BrowserSession:
    def __init__(self, page_load_timeout: float = 30, headless: bool = False) -> None:
        self._timeout = page_load_timeout
        self._headless = headless
        self._driver: webdriver.Chrome | None = None

    def __enter__(self) -> BrowserSession:
        options = Options()
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--lang=en-GB")
        if self._headless:
            options.add_argument("--headless=new")
        else:
            options.add_argument("--start-maximized")
        self._driver = webdriver.Chrome(options=options)
        self._driver.set_page_load_timeout(self._timeout)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except WebDriverException:
                log.warning("Chrome did not quit cleanly", exc_info=True)
            self._driver = None

    @property
    def driver(self) -> webdriver.Chrome:
        if self._driver is None:
            raise RuntimeError("BrowserSession must be used inside a 'with' block")
        return self._driver

    @property
    def current_url(self) -> str:
        return self.driver.current_url

    def open(self, url: str) -> None:
        self.driver.get(url)

    def html(self) -> str:
        return self.driver.page_source

    def scroll_to_load(self) -> None:
        """Scroll down so lazy-loaded content (often contact details) renders."""
        try:
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight/2);")
            _pause(1, 2)
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            _pause(2, 4)
        except WebDriverException:
            pass


def _is_google(host: str) -> bool:
    return host == "google.com" or host.endswith(".google.com") or host.startswith("google.")


def _result_url(block) -> str | None:
    """Return the site a result points to, or None if it is not an outside site.

    Google often wraps organic links in /goto redirects, which carry no target
    address. The breadcrumb (<cite>) above the title shows the site instead,
    for example "https://example.com › write-for-us". Only its base address is used.
    """
    anchors = block.find_elements(By.CSS_SELECTOR, "a[href]")
    if anchors:
        href = anchors[0].get_attribute("href") or ""
        if href.startswith("http") and not _is_google(domain_of(href)):
            return href

    cites = block.find_elements(By.CSS_SELECTOR, "cite")
    if not cites:
        return None
    parts = cites[0].text.split()
    if not parts:
        return None
    site = parts[0]
    url = site if site.startswith("http") else f"https://{site}"
    host = domain_of(url)
    if _is_google(host) or not is_plausible_domain(host):
        return None
    return url


def _type_slowly(element, text: str) -> None:
    """Type with human-like pauses."""
    for i, char in enumerate(text):
        element.send_keys(char)
        if i % random.randint(3, 7) == 0:
            time.sleep(random.uniform(0.2, 0.6))
        else:
            time.sleep(random.uniform(0.05, 0.15))


class GoogleSearch:
    HOME_URL = "https://www.google.co.uk/?hl=en-GB&gl=uk&pws=0"
    RESULT_SELECTOR = "div.g, div.MjjYud"

    def __init__(self, session: BrowserSession, max_pages: int = 5) -> None:
        self._session = session
        self._max_pages = max_pages

    def run(self, query: str) -> list[SearchResult]:
        driver = self._session.driver
        self._session.open(self.HOME_URL)
        _pause(2, 5)

        box = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.NAME, "q")))
        _pause(1, 2)
        _type_slowly(box, query)
        _pause(1, 2)
        box.send_keys(Keys.RETURN)
        _pause(1.5, 3.5)

        results: list[SearchResult] = []
        for page_number in range(1, self._max_pages + 1):
            _pause(2, 4)
            results.extend(self._parse_results())
            if page_number == self._max_pages or not self._go_next():
                break
        return results

    def _parse_results(self) -> list[SearchResult]:
        if "/sorry/" in self._session.current_url:
            raise SearchBlockedError("Google is showing a CAPTCHA. Stop and retry later.")

        results: list[SearchResult] = []
        blocks = self._session.driver.find_elements(By.CSS_SELECTOR, self.RESULT_SELECTOR)
        for block in blocks:
            url = _result_url(block)
            if url is None:
                continue
            results.append(SearchResult(url=url, domain=domain_of(url), snippet=block.text))
        return results

    def _go_next(self) -> bool:
        try:
            button = self._session.driver.find_element(By.ID, "pnnext")
        except NoSuchElementException:
            return False
        self._session.driver.execute_script("arguments[0].click();", button)
        _pause(3, 6)
        return True


class HtmlParser(Protocol):
    def parse(self, html: str) -> PageData: ...


class PageReader:
    """Loads a page in the browser (so JavaScript runs) and parses it."""

    def __init__(self, session: BrowserSession, parser: HtmlParser) -> None:
        self._session = session
        self._parser = parser

    def read(self, url: str) -> PageData:
        self._session.open(url)
        self._session.scroll_to_load()
        return self._parser.parse(self._session.html())
