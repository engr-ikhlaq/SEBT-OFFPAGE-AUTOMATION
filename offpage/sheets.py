"""Google Sheets output: header setup and retried appends."""
from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

from .models import LEAD_HEADERS
from .writer import SinkError

log = logging.getLogger(__name__)

SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
)


def open_worksheet(
    credentials_path: Path,
    spreadsheet_id: str,
    worksheet_name: str | None = None,
) -> gspread.Worksheet:
    creds = Credentials.from_service_account_file(str(credentials_path), scopes=SCOPES)
    spreadsheet = gspread.authorize(creds).open_by_key(spreadsheet_id)
    return spreadsheet.worksheet(worksheet_name) if worksheet_name else spreadsheet.sheet1


class SheetSink:
    def __init__(
        self,
        worksheet: gspread.Worksheet,
        headers: Sequence[str] = LEAD_HEADERS,
        attempts: int = 4,
        base_delay: float = 2.0,
    ) -> None:
        self._ws = worksheet
        self._headers = list(headers)
        self._attempts = attempts
        self._base_delay = base_delay

    def ensure_headers(self) -> None:
        if not any(self._ws.row_values(1)):
            last_cell = gspread.utils.rowcol_to_a1(1, len(self._headers))
            self._ws.update(range_name=f"A1:{last_cell}", values=[self._headers])

    def append_rows(self, rows: list[list[str | int]]) -> None:
        for attempt in range(1, self._attempts + 1):
            try:
                # RAW keeps scraped text as text, so a value starting with "=" is not run as a formula.
                self._ws.append_rows(rows, value_input_option="RAW")
                return
            except (gspread.exceptions.APIError, OSError) as exc:
                if attempt == self._attempts:
                    raise SinkError(f"Sheets append failed after {attempt} attempts") from exc
                delay = self._base_delay * 2 ** (attempt - 1)
                log.warning("Sheets append failed (%s); retrying in %.0fs", exc, delay)
                time.sleep(delay)
