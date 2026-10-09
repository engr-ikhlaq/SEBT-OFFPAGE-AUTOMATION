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

import data_source
from offpage.browser import BrowserSession, GoogleSearch, PageReader
from offpage.config import Settings
from offpage.crawler import SiteCrawler
from offpage.filters import UrlFilter
from offpage.parsing import PageParser
from offpage.pipeline import LeadPipeline
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


@dataclasses.dataclass
class ScrapeStatus:
    # idle | running | awaiting_decision | finished | stopped | error
    state: str = "idle"
    keyword: str = ""
    username: str = ""
    queued_this_batch: int = 0
    queued_total: int = 0
    last_domain: str = ""
    error: str = ""
    backend_name: str = ""


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


def start(keyword: str, username: str, is_owner: bool) -> bool:
    """Starts a fresh scrape for `keyword`, writing to the backend
    data_source.for_user(username, is_owner) resolves. False if one is
    already busy (one scrape, one Chrome window, at a time)."""
    return _start(keyword, username, is_owner, reset_total=True)


def continue_scraping() -> bool:
    """Starts another batch for the keyword/user a prior batch was awaiting a decision on."""
    with _lock:
        keyword, username = _status.keyword, _status.username
        ready = _status.state == "awaiting_decision" and bool(keyword)
    return _start(keyword, username, _last_is_owner, reset_total=False) if ready else False


def stop() -> None:
    """Asks a running batch to stop at its next opportunity (does not kill mid-search)."""
    global _stop_requested
    with _lock:
        _stop_requested = True


_last_is_owner = False  # remembered so continue_scraping() can re-resolve the same user's backend


def _start(keyword: str, username: str, is_owner: bool, reset_total: bool) -> bool:
    global _thread, _stop_requested, _last_is_owner
    with _lock:
        if _status.state == "running":
            return False
        _stop_requested = False
        _last_is_owner = is_owner
        _status.state = "running"
        _status.keyword = keyword
        _status.username = username
        _status.queued_this_batch = 0
        if reset_total:
            _status.queued_total = 0
        _status.last_domain = ""
        _status.error = ""
    _thread = threading.Thread(target=_run_batch, args=(keyword, username, is_owner), daemon=True)
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
        max_pages=int(os.environ.get("SCRAPE_MAX_PAGES", "1")),
        keywords=(keyword,),
    )


def _on_lead(count: int, lead) -> None:
    with _lock:
        _status.queued_this_batch = count
        _status.queued_total += 1
        _status.last_domain = lead.domain


def _should_stop() -> bool:
    with _lock:
        return _stop_requested


def _run_batch(keyword: str, username: str, is_owner: bool) -> None:
    settings = _settings_for_keyword(keyword)
    backend = data_source.for_user(username, is_owner)
    with _lock:
        _status.backend_name = backend.name

    http = requests.Session()
    http.headers["User-Agent"] = USER_AGENT
    parser = PageParser()
    scorer = Scorer()
    writer: LeadWriter | None = None

    try:
        with SeenStore(settings.seen_db_path) as store, BrowserSession() as session:
            sink = backend.make_sink()
            sink.ensure_headers()
            writer = LeadWriter(sink, store, settings.batch_size)

            pipeline = LeadPipeline(
                settings=settings,
                search=GoogleSearch(session, settings.max_pages),
                reader=PageReader(session, parser),
                crawler=SiteCrawler(http, parser, workers=settings.crawl_workers),
                scorer=scorer,
                gate=RelevanceGate(scorer),
                email_validator=EmailValidator(),
                url_filter=UrlFilter(settings.allowed_suffixes),
                store=store,
                writer=writer,
            )
            pipeline.run(max_leads=LEADS_PER_BATCH, on_lead=_on_lead, should_stop=_should_stop)
    except Exception as exc:
        log.exception("Scrape job failed")
        with _lock:
            _status.state = "error"
            _status.error = str(exc)
        return
    finally:
        if writer is not None:
            try:
                writer.flush()
            except Exception:
                log.exception("Could not save pending leads")

    with _lock:
        if _stop_requested:
            _status.state = "stopped"
        elif _status.queued_this_batch == 0:
            _status.state = "finished"
        else:
            _status.state = "awaiting_decision"
