"""
sheet_store.py
A Google Sheet as a lead store — works with ANY already-open
gspread.Worksheet, however it was authorized: the admin's shared Sheet
(service account) or a user's own personal Sheet (their OAuth
credentials, see google_oauth.py). Same column layout as local_store.py,
so a user's local data can migrate into their Sheet with no conversion
(see data_source.migrate_to_sheet).

This is deliberately separate from sheets_source.py, which is specifically
the admin's env-configured Sheet (GOOGLE_SHEET_ID/GOOGLE_SERVICE_ACCOUNT_FILE)
and is also used by the manual campaign form's "pull recipients from the
connected Sheet" checkbox — a different feature this module doesn't touch.
"""

from __future__ import annotations

from datetime import datetime, timezone

import gspread

import local_store

HEADERS = local_store.HEADERS  # one definition, shared with the local-file path


class SheetStore:
    """What a user's personally-connected Sheet is accessed through — both
    as the scraper's RowSink (ensure_headers/append_rows) and for reading/
    writing outreach status, same shape as local_store's module functions."""

    def __init__(self, worksheet: gspread.Worksheet) -> None:
        self._ws = worksheet

    # --- RowSink protocol (offpage.writer.LeadWriter writes through this) ---

    def ensure_headers(self) -> None:
        current = [cell.strip() for cell in self._ws.row_values(1)]
        if not any(current):
            last_cell = gspread.utils.rowcol_to_a1(1, len(HEADERS))
            self._ws.update(range_name=f"A1:{last_cell}", values=[list(HEADERS)])

    def append_rows(self, rows: list[list]) -> None:
        self._ws.append_rows(rows, value_input_option="RAW")

    # --- outreach / dashboard reads ---

    def read_recipients(self, skip_already_sent: bool = True) -> list[dict]:
        records = self._ws.get_all_records()
        header = self._ws.row_values(1)
        out = []
        for i, record in enumerate(records, start=2):
            email = str(record.get("Email", "")).strip()
            if not email:
                continue
            if skip_already_sent and str(record.get("Status", "")).strip().lower() == "sent":
                continue
            record["row"] = i
            out.append(record)
        return out

    def write_result(self, row: int, status: str, error: str | None = None) -> None:
        header = self._ws.row_values(1)
        col_status = header.index("Status") + 1
        col_sent_time = header.index("Sent Time") + 1
        col_error = header.index("Error") + 1

        sent_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if status == "sent" else ""
        self._ws.update_cell(row, col_status, status)
        self._ws.update_cell(row, col_sent_time, sent_time)
        self._ws.update_cell(row, col_error, error or "")

    def get_lead_counts(self) -> dict:
        counts = {"total": 0, "sent": 0, "failed": 0, "pending": 0}
        for record in self.read_recipients(skip_already_sent=False):
            status = str(record.get("Status", "")).strip().lower()
            counts["total"] += 1
            counts[status if status in ("sent", "failed") else "pending"] += 1
        return counts
