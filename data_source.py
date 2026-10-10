"""
data_source.py
Resolves where a SPECIFIC user's leads live — this used to be one global
choice for the whole app, which was wrong: it meant a brand-new user's
scraped data would land in the admin's shared Sheet the moment one was
configured, with no way to keep it separate. Now every user gets their
own, resolved in this order:

1. Their own connected Sheet (OAuth, see google_oauth.py) — their own
   Google Drive, their own credentials, nobody else's data mixes in.
2. The admin's env-configured shared Sheet (GOOGLE_SHEET_ID in .env) —
   ONLY for the owner account itself, since that's who set it up. A new,
   non-owner user never inherits this just because it exists.
3. Otherwise: a local Excel file unique to that user
   (local_store.user_leads_path) — everyone starts here until they
   connect something of their own.

When a user connects their own Sheet, anything sitting in their local
file is migrated into it automatically (migrate_to_sheet) — nothing is
silently left behind or duplicated.

scrape_job.py, auto_outreach.py and the dashboard all call for_user()
instead of importing sheets_source/local_store/sheet_store directly, so
there's exactly one place that makes this decision.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import google_oauth
import local_store
import sheets_source
from sheet_store import SheetStore


class _SheetsSourceBackend:
    """Adapts sheets_source's module-level functions (the admin's
    service-account-backed Sheet) to the common backend shape."""

    def __init__(self, label: str) -> None:
        self.name = label

    def read_recipients(self, skip_already_sent: bool = True) -> list[dict]:
        return sheets_source.read_recipients(skip_already_sent=skip_already_sent)

    def write_result(self, row: int, status: str, error: str | None = None) -> None:
        sheets_source.write_result(row, status=status, error=error)

    def get_lead_counts(self) -> dict:
        return sheets_source.get_lead_counts()

    def make_sink(self):
        from offpage.sheets import SheetSink, open_worksheet
        import os
        ws = open_worksheet(
            Path(os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "sheet_credentials.json")),
            os.environ["GOOGLE_SHEET_ID"],
            os.environ.get("GOOGLE_WORKSHEET_NAME") or None,
        )
        return SheetSink(ws)


class _LocalBackend:
    """A specific local Excel file — the per-user default, or the shared
    LOCAL_LEADS_FILE fallback (see for_user())."""

    def __init__(self, path: Path, label: str) -> None:
        self._path = path
        self.name = label

    def read_recipients(self, skip_already_sent: bool = True) -> list[dict]:
        return local_store.read_recipients(skip_already_sent=skip_already_sent, path=self._path)

    def write_result(self, row: int, status: str, error: str | None = None) -> None:
        local_store.write_result(row, status=status, error=error, path=self._path)

    def get_lead_counts(self) -> dict:
        return local_store.get_lead_counts(path=self._path)

    def make_sink(self):
        return local_store.ExcelSink(path=self._path)


class _UserSheetBackend:
    """A user's own personally-connected Sheet (OAuth)."""

    def __init__(self, store: SheetStore, label: str) -> None:
        self._store = store
        self.name = label

    def read_recipients(self, skip_already_sent: bool = True) -> list[dict]:
        return self._store.read_recipients(skip_already_sent=skip_already_sent)

    def write_result(self, row: int, status: str, error: str | None = None) -> None:
        self._store.write_result(row, status=status, error=error)

    def get_lead_counts(self) -> dict:
        return self._store.get_lead_counts()

    def make_sink(self):
        return self._store


def admin_sheet_configured() -> bool:
    return sheets_source.is_configured()


def service_account_sheets_configured() -> bool:
    """True if GOOGLE_SERVICE_ACCOUNT_FILE is set up, so a user can connect
    their own Sheet (see /connect/sheet) without needing OAuth Cloud
    Console setup, which only the owner can do."""
    return sheets_source.service_account_email() is not None


def open_worksheet_by_id(sheet_id: str, worksheet_name: str | None):
    """Opens any Sheet (not necessarily the admin's) the service account
    has Editor access to — used both by for_user() for a user's own
    connected Sheet, and by /connect/sheet to validate one before saving it."""
    from offpage.sheets import open_worksheet
    creds_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
    return open_worksheet(Path(creds_file), sheet_id, worksheet_name or None)


# Resolving a Sheet-backed user means a real Sheets API call just to OPEN
# it (gspread's open_by_key() does a metadata fetch) - before a single
# lead is even read. for_user() gets called on nearly every request
# (Compose page load, /leads/recent, /dashboard, each polled every couple
# of seconds while a scrape is running), so re-opening on every single
# call burns through Google's ~60-reads/minute-per-user quota fast and
# turns into a 429 that previously crashed the page outright. Caching the
# resolved backend for a short window means a poll loop reuses the same
# already-open connection instead of re-opening it every tick.
_BACKEND_CACHE_TTL = 20.0
_backend_cache: dict[str, tuple[float, object]] = {}


def invalidate_user_backend_cache(username: str) -> None:
    """Call right after anything that changes WHICH backend a user
    resolves to (connecting/disconnecting a Sheet) so the next request
    picks up the change immediately instead of waiting out the TTL."""
    _backend_cache.pop(f"{username}:True", None)
    _backend_cache.pop(f"{username}:False", None)


def for_user(username: str, is_owner: bool):
    """The backend THIS user's leads live in — see module docstring for
    the priority order. Returns an object with read_recipients(),
    write_result(), get_lead_counts(), make_sink(), and a .name for display.
    Cached briefly per (username, is_owner) — see _BACKEND_CACHE_TTL."""
    cache_key = f"{username}:{is_owner}"
    cached = _backend_cache.get(cache_key)
    if cached is not None and time.monotonic() - cached[0] < _BACKEND_CACHE_TTL:
        return cached[1]

    backend = _resolve_backend(username, is_owner)
    _backend_cache[cache_key] = (time.monotonic(), backend)
    return backend


def _resolve_backend(username: str, is_owner: bool):
    import db

    account = db.get_google_account(username)
    if account and account.get("sheet_id"):
        creds = google_oauth.credentials_from_row(account)
        ws = google_oauth.open_user_worksheet(creds, account["sheet_id"])
        return _UserSheetBackend(SheetStore(ws), f"your connected Sheet ({account['google_email']})")

    user_sheet = db.get_user_sheet(username)
    if user_sheet:
        ws = open_worksheet_by_id(user_sheet["sheet_id"], user_sheet.get("worksheet_name"))
        return _UserSheetBackend(SheetStore(ws), "your connected Sheet")

    if is_owner and admin_sheet_configured():
        return _SheetsSourceBackend("the shared Google Sheet")

    return _LocalBackend(local_store.user_leads_path(username), "your local file")


def migrate_to_sheet(username: str, backend: "_UserSheetBackend") -> int:
    """Copies any leads sitting in this user's local file into the Sheet
    they just connected, then clears the local file — called right after
    a successful OAuth connect (see app.py). Returns how many rows moved.
    A no-op if they had no local file yet."""
    local_path = local_store.user_leads_path(username)
    if not local_path.exists():
        return 0
    rows = local_store.read_all_rows(local_path)
    if not rows:
        local_store.clear(local_path)
        return 0
    sink = backend.make_sink()
    sink.ensure_headers()
    sink.append_rows(rows)
    local_store.clear(local_path)
    return len(rows)
