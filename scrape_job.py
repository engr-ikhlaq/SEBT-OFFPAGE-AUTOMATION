"""
scrape_job.py
Runs the offpage lead-scraper for one keyword in a background thread, so a
web request can start it and return immediately. The web side polls
get_status() rather than anything pushing to it - keeps the browser side to
plain fetch() on a timer, no websockets.

Only one scrape runs at a time - a second real Chrome window driving Google
searches at the same time isn't something to stack, so start() refuses while
one is already running or waiting on a decision.

Batches of LEADS_PER_BATCH leads: after each batch, the job pauses in
"awaiting_decision" so the page can ask the user whether to keep scraping or
switch to emailing what's been found. continue_scraping() starts the next
batch for the same keyword - already-seen domains are skipped automatically
(offpage.storage.SeenStore), so it picks up where the batch before left off
rather than repeating it.

Where results get written: start() takes who's scraping (username,
is_owner) and resolves their backend via data_source.for_user() - their
own connected Sheet, the admin's shared Sheet (owner only), or their own
local file. Two different users running a scrape never write to the same
place unless they've both connected the same Sheet on purpose. The
seen-domains dedup database stays shared across everyone, though - that's
just "don't re-check a site we already looked at," independent of whose
lead data it ends up in.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import threading
from pathlib import Path

import requests
from selenium.common.exceptions import InvalidSessionIdException

import data_source
import db
from offpage.authority import AuthorityChecker
from offpage.browser import BrowserSession, GoogleSearch, PageReader
from offpage.config import Settings
from offpage.crawler import SiteCrawler
from offpage.filters import UrlFilter
from offpage.parsing import PageParser
from offpage.pipeline import LeadPipeline
from offpage.regions import DEFAULT_COUNTRY, DEFAULT_LANGUAGE, get_country, get_language, home_url
from offpage.relevance import RelevanceGate
from offpage.scoring import Scorer
from offpage.storage import SeenStore
from offpage.validation import EmailValidator
from offpage.writer import LeadWriter

log = logging.getLogger("scrape_job")

LEADS_PER_BATCH = 100
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Chrome always runs on THIS machine (wherever app.py is running) - a web
# request can't open a window on the browser making it, no matter who's
# logged in or where they're connecting from. Visible by default so
# whoever is physically at this machine can see a Google CAPTCHA if one
# comes up and solve it by hand - that's the only real fix for one, and
# this app makes no attempt to bypass a CAPTCHA automatically. Set
# SCRAPE_HEADLESS=true in .env for an unattended server with no one
# around to see the window anyway.
_HEADLESS = os.environ.get("SCRAPE_HEADLESS", "false").strip().lower() not in ("false", "0", "no")


@dataclasses.dataclass
class ScrapeStatus:
    # idle | running | awaiting_decision | finished | stopped | error
    state: str = "idle"
    keyword: str = ""
    username: str = ""
    country: str = ""  # Google `gl` code, see offpage/regions.py
    language: str = ""  # Google `hl` code, see offpage/regions.py
    queued_this_batch: int = 0
    queued_total: int = 0
    last_domain: str = ""
    error: str = ""
    backend_name: str = ""
    # True while state is still "running" but Google is showing a CAPTCHA
    # and the scraper is waiting (see offpage.browser.GoogleSearch) for it
    # to be solved by hand in the Chrome window - lets the page say so
    # instead of just looking stuck on the same count.
    waiting_captcha: bool = False


_lock = threading.Lock()
_status = ScrapeStatus()
_stop_requested = False
_thread: threading.Thread | None = None


def get_status() -> dict:
    with _lock:
        return dataclasses.asdict(_status)


def is_busy() -> bool:
    with _lock:
        return _status.state == "running"


def start(keyword: str, username: str, is_owner: bool,
          country: str = DEFAULT_COUNTRY, language: str = DEFAULT_LANGUAGE) -> bool:
    """Starts a fresh scrape for `keyword` targeting `country`/`language`
    (codes from offpage/regions.py - unrecognized or missing falls back to
    the United Kingdom/English, this app's long-standing default), writing
    to the backend data_source.for_user(username, is_owner) resolves.
    False if one is already busy (one scrape, one Chrome window, at a
    time) OR a previous batch is still paused awaiting a decision -
    starting a new one right over it would silently discard whatever that
    batch was waiting on (see continue_scraping()/dismiss_decision() for
    the two ways to actually resolve it first)."""
    with _lock:
        if _status.state in ("running", "awaiting_decision"):
            return False
    return _start(keyword, username, is_owner, reset_total=True, country=country, language=language)


def continue_scraping() -> bool:
    """Starts another batch for the keyword/user/region a prior batch was awaiting a decision on."""
    with _lock:
        keyword, username = _status.keyword, _status.username
        country, language = _status.country, _status.language
        ready = _status.state == "awaiting_decision" and bool(keyword)
    return (
        _start(keyword, username, _last_is_owner, reset_total=False, country=country, language=language)
        if ready else False
    )


def dismiss_decision() -> bool:
    """The "Stop" choice on a paused ("awaiting_decision") batch: clears it
    back to idle without starting anything new. Whatever was already found
    is already saved (LeadPipeline writes as it goes) - this only resets
    the status so the Compose page's idle form is usable again instead of
    staying stuck showing a paused batch with no running thread behind it.
    False if nothing is actually paused."""
    with _lock:
        if _status.state != "awaiting_decision":
            return False
        _status.state = "idle"
        _status.keyword = ""
        _status.last_domain = ""
        _status.queued_this_batch = 0
    return True


def stop() -> None:
    """Asks a running batch to stop at its next opportunity (does not kill mid-search)."""
    global _stop_requested
    with _lock:
        _stop_requested = True


_last_is_owner = False  # remembered so continue_scraping() can re-resolve the same user's backend


def _start(keyword: str, username: str, is_owner: bool, reset_total: bool,
           country: str = DEFAULT_COUNTRY, language: str = DEFAULT_LANGUAGE) -> bool:
    global _thread, _stop_requested, _last_is_owner
    country = get_country(country)  # normalizes/validates once, up front
    language = get_language(language)
    with _lock:
        if _status.state == "running":
            return False
        _stop_requested = False
        _last_is_owner = is_owner
        _status.state = "running"
        _status.keyword = keyword
        _status.username = username
        _status.country = country
        _status.language = language
        _status.queued_this_batch = 0
        if reset_total:
            _status.queued_total = 0
        _status.last_domain = ""
        _status.error = ""
        _status.waiting_captcha = False
    # Logged now, not just once the run finishes - an admin should be able
    # to see a scrape that's still running, or that crashed outright,
    # not only the ones that completed cleanly.
    history_id = db.record_search_start(username, keyword, country, language)
    _thread = threading.Thread(
        target=_run_batch, args=(keyword, username, is_owner, country, language, history_id), daemon=True
    )
    _thread.start()
    return True


def _settings_for_keyword(keyword: str) -> Settings:
    """Config comes from this app's own .env (GOOGLE_*), not offpage's usual
    SPREADSHEET_ID/CREDENTIALS_PATH vars - avoids two names for one value.

    spreadsheet_id is a placeholder here and never actually used - offpage's
    Settings requires a non-empty value, but which sink the pipeline writes
    to comes from data_source.for_user(), not from this Settings object
    (see _run_batch).
    """
    return Settings(
        spreadsheet_id="unused-see-data-source-for-user",
        seen_db_path=Path(os.environ.get("SEEN_DB_PATH", "seen_domains.db")),
        max_pages=int(os.environ.get("SCRAPE_MAX_PAGES", "5")),
        keywords=(keyword,),
    )


def _authority_checker() -> AuthorityChecker:
    """A fresh checker per scrape run (its in-process cache doesn't need
    to outlive one run - the next run re-checking a domain isn't a real
    cost). OPEN_PAGERANK_API_KEY unset just means every lead's DA column
    stays blank; Tranco traffic rank needs no key at all."""
    return AuthorityChecker(open_pagerank_key=os.environ.get("OPEN_PAGERANK_API_KEY"))


def _authority_thresholds() -> tuple[float, int | None]:
    """(min_da, max_traffic_rank) from .env - both unset (0, None) means
    no filtering at all, the same as before this feature existed: every
    lead that passes relevance/email still gets through, just now with
    DA/traffic shown alongside it. Set these once you've seen what real
    numbers look like for your niche and know what "too low" means for it."""
    min_da = float(os.environ.get("MIN_DA_SCORE", "0") or "0")
    raw_rank = os.environ.get("MAX_TRAFFIC_RANK", "").strip()
    max_traffic_rank = int(raw_rank) if raw_rank else None
    return min_da, max_traffic_rank


def _on_lead(count: int, lead) -> None:
    with _lock:
        _status.queued_this_batch = count
        _status.queued_total += 1
        _status.last_domain = lead.domain


def _on_captcha(waiting: bool) -> None:
    with _lock:
        _status.waiting_captcha = waiting


def _should_stop() -> bool:
    with _lock:
        return _stop_requested


def _run_batch(keyword: str, username: str, is_owner: bool,
                country: str = DEFAULT_COUNTRY, language: str = DEFAULT_LANGUAGE,
                history_id: int | None = None) -> None:
    settings = _settings_for_keyword(keyword)
    search_url = home_url(country, language)
    backend = data_source.for_user(username, is_owner)
    with _lock:
        _status.backend_name = backend.name

    http = requests.Session()
    http.headers["User-Agent"] = USER_AGENT
    parser = PageParser()
    scorer = Scorer()

    try:
        with SeenStore(settings.seen_db_path) as store, \
             BrowserSession(headless=_HEADLESS, lang=language) as session:
            sink = backend.make_sink()
            sink.ensure_headers()
            # async_writes=True: each lead's row is saved on a background
            # thread the instant it's confirmed, in parallel with the
            # scraper moving straight on to the next search/page instead
            # of blocking on the Sheets API round trip first - the Sheet
            # and the scrape progress stay in step without the write
            # latency adding to how long the scrape itself takes.
            writer = LeadWriter(sink, store, settings.batch_size, async_writes=True)
            min_da, max_traffic_rank = _authority_thresholds()

            pipeline = LeadPipeline(
                settings=settings,
                search=GoogleSearch(session, settings.max_pages, home_url=search_url,
                                     should_stop=_should_stop, on_captcha=_on_captcha),
                reader=PageReader(session, parser),
                crawler=SiteCrawler(http, parser, workers=settings.crawl_workers),
                scorer=scorer,
                gate=RelevanceGate(scorer),
                email_validator=EmailValidator(),
                url_filter=UrlFilter(settings.allowed_suffixes),
                store=store,
                writer=writer,
                authority_checker=_authority_checker(),
                min_da=min_da,
                max_traffic_rank=max_traffic_rank,
            )
            try:
                pipeline.run(max_leads=LEADS_PER_BATCH, on_lead=_on_lead, should_stop=_should_stop)
            finally:
                # Must happen INSIDE this `with` block, before store/session
                # close - the background write thread needs both still open
                # to finish saving whatever's still queued, whether this
                # batch ended cleanly, was stopped, or the browser itself
                # failed (a Sheets API write has nothing to do with Chrome,
                # so it's still worth finishing even then).
                try:
                    writer.flush()
                    writer.wait_all()
                except Exception:
                    log.exception("Could not save all pending leads")
                finally:
                    writer.close()
    except InvalidSessionIdException:
        # Raised when the Chrome window itself is gone mid-run - closed
        # (by hand or by the OS), crashed, or killed - rather than any
        # search/page failure. selenium's own message for this is a huge
        # native-stack-trace dump that's meaningless to someone reading it
        # in a browser alert, so it's swapped for a plain explanation.
        # Only reachable when SCRAPE_HEADLESS=false, since a headless
        # Chrome has no window for anyone to close.
        log.exception("Scrape job failed: browser window closed mid-run")
        with _lock:
            _status.state = "error"
            _status.error = ("The Chrome window closed before scraping finished - it may have been "
                              "closed by hand, or crashed. Leave it open until a batch completes (use "
                              "the Stop button here instead of closing the window), then start again.")
            leads_found = _status.queued_total
        if history_id is not None:
            db.record_search_finish(history_id, leads_found, "error")
        return
    except Exception as exc:
        log.exception("Scrape job failed")
        with _lock:
            _status.state = "error"
            _status.error = str(exc)
            leads_found = _status.queued_total
        if history_id is not None:
            db.record_search_finish(history_id, leads_found, "error")
        return

    with _lock:
        if _stop_requested:
            _status.state = "stopped"
        elif _status.queued_this_batch == 0:
            _status.state = "finished"
        else:
            _status.state = "awaiting_decision"
        final_state = _status.state
        leads_found = _status.queued_total
    if history_id is not None:
        db.record_search_finish(history_id, leads_found, final_state)
