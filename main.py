"""Entry point: find guest-post targets and record them in Google Sheets."""
from __future__ import annotations

import argparse
import logging
from dataclasses import replace

import requests
from selenium.common.exceptions import InvalidSessionIdException

from offpage.browser import BrowserSession, GoogleSearch, PageReader, SearchBlockedError
from offpage.config import ConfigError, Settings
from offpage.crawler import SiteCrawler
from offpage.filters import UrlFilter
from offpage.parsing import PageParser
from offpage.pipeline import LeadPipeline, SearchFailuresError
from offpage.relevance import RelevanceGate
from offpage.scoring import Scorer
from offpage.sheets import SheetSink, open_worksheet
from offpage.storage import SeenStore
from offpage.validation import EmailValidator
from offpage.writer import LeadWriter

log = logging.getLogger("offpage")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find guest-post targets and record them.")
    parser.add_argument("--max-pages", type=int, help="Google result pages per query")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    if args.max_pages is not None:
        settings = replace(settings, max_pages=args.max_pages)

    http = requests.Session()
    http.headers["User-Agent"] = USER_AGENT
    parser = PageParser()
    scorer = Scorer()
    exit_code = 0

    with SeenStore(settings.seen_db_path) as store, BrowserSession() as session:
        sink = SheetSink(
            open_worksheet(settings.credentials_path, settings.spreadsheet_id, settings.worksheet_name)
        )
        sink.ensure_headers()
        writer = LeadWriter(sink, store, settings.batch_size)

        pipeline = LeadPipeline(
            settings=settings,
            search=GoogleSearch(session, settings.max_pages),
            reader=PageReader(session, parser),
            crawler=SiteCrawler(http, parser),
            scorer=scorer,
            gate=RelevanceGate(scorer),
            email_validator=EmailValidator(),
            url_filter=UrlFilter(settings.allowed_suffixes),
            store=store,
            writer=writer,
        )
        try:
            queued = pipeline.run()
            log.info("Queued %d new lead(s)", queued)
        except KeyboardInterrupt:
            log.warning("Stopped by user")
            exit_code = 130
        except SearchBlockedError as exc:
            log.error("%s", exc)
            exit_code = 1
        except SearchFailuresError as exc:
            log.error("%s", exc)
            exit_code = 1
        except InvalidSessionIdException:
            log.error("Chrome closed during the run. Restart main.py to continue.")
            exit_code = 1
        finally:
            _save_pending(writer)

    return exit_code


def _save_pending(writer: LeadWriter) -> None:
    if writer.pending == 0:
        return
    try:
        saved = writer.flush()
        log.info("Saved %d pending lead(s)", saved)
    except Exception:
        log.exception(
            "Could not save %d pending lead(s). Their domains stay unseen and will be retried next run.",
            writer.pending,
        )


if __name__ == "__main__":
    raise SystemExit(main())
