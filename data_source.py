"""
data_source.py
Picks where scraped leads + outreach status live: the shared Google Sheet
if one's configured (GOOGLE_SHEET_ID/GOOGLE_SERVICE_ACCOUNT_FILE in .env —
see sheets_source.py), otherwise local_store.py's always-available Excel
file. Everything that reads or writes lead data — scrape_job.py,
auto_outreach.py, the dashboard — goes through this instead of importing
sheets_source or local_store directly, so there is exactly one place that
decides which backend is active, and switching is just: set the env vars,
restart.
"""

from __future__ import annotations

import local_store
import sheets_source


def is_sheet_connected() -> bool:
    return sheets_source.is_configured()


def _backend():
    return sheets_source if is_sheet_connected() else local_store


def backend_name() -> str:
    return "Google Sheet" if is_sheet_connected() else "local Excel file"


def get_lead_counts() -> dict:
    return _backend().get_lead_counts()


def read_recipients(skip_already_sent: bool = True) -> list[dict]:
    return _backend().read_recipients(skip_already_sent=skip_already_sent)


def write_result(row: int, status: str, error: str | None = None) -> None:
    _backend().write_result(row, status=status, error=error)
