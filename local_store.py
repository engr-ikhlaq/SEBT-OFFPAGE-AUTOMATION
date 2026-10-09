"""
local_store.py
The default, always-available home for scraped leads + outreach status: a
local Excel file (leads.xlsx), created automatically on first use. No
setup, no credentials, no sharing a sheet with anyone — works the moment
the app starts.

Same column layout as the Google Sheet path (sheets_source.py / the
Keywords tab), so the two are interchangeable: data_source.py picks
whichever is active, and nothing that reads or writes lead data needs to
know which one it's actually talking to.

ExcelSink implements the same two methods offpage.sheets.SheetSink does
(ensure_headers, append_rows) — the RowSink protocol offpage.writer.
LeadWriter already expects — so the scraper writes to this exactly the
way it writes to a real Sheet, with no changes to offpage/ itself.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook, load_workbook

HEADERS = (
    "Date", "Keyword", "Query", "Domain", "URL",
    "Email", "Contact Page", "Snippet", "Score",
    "Status", "Sent Time", "Error",
)
SHEET_NAME = "Leads"

# openpyxl has no concurrent-writer story of its own (it's a file format
# library, not a database) — this app can have the scraper and a manual
# outreach send both wanting to write around the same time, so every
# read-modify-write below goes through one lock.
_lock = threading.Lock()


def _path() -> Path:
    return Path(os.environ.get("LOCAL_LEADS_FILE", "leads.xlsx"))


def _open_or_create() -> Workbook:
    path = _path()
    if path.exists():
        return load_workbook(path)
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    ws.append(list(HEADERS))
    wb.save(path)
    return wb


def _worksheet(wb: Workbook):
    return wb[SHEET_NAME] if SHEET_NAME in wb.sheetnames else wb.active


class ExcelSink:
    """What the scraper (offpage.writer.LeadWriter) actually writes
    through — see module docstring."""

    def ensure_headers(self) -> None:
        with _lock:
            _open_or_create()  # creates the file with headers if it's missing; otherwise a no-op

    def append_rows(self, rows: list[list]) -> None:
        with _lock:
            wb = _open_or_create()
            ws = _worksheet(wb)
            for row in rows:
                ws.append(row)
            wb.save(_path())


def is_configured() -> bool:
    """Always True — this is the no-setup fallback, not an optional feature."""
    return True


def read_recipients(skip_already_sent: bool = True) -> list[dict]:
    """Same shape as sheets_source.read_recipients: a list of row dicts
    (keyed by header) with a 'row' key giving the actual spreadsheet row
    number, for write_result() to target later."""
    with _lock:
        wb = _open_or_create()
        rows = list(_worksheet(wb).iter_rows(values_only=True))

    if not rows:
        return []
    header = [str(h) if h is not None else "" for h in rows[0]]

    out = []
    for i, values in enumerate(rows[1:], start=2):
        # openpyxl reads a blank cell as None, not "" — normalize once here
        # so every caller gets plain strings, the same as the Sheets path.
        record = {key: ("" if value is None else value) for key, value in zip(header, values)}
        email = str(record.get("Email") or "").strip()
        if not email:
            continue
        if skip_already_sent and str(record.get("Status") or "").strip().lower() == "sent":
            continue
        record["row"] = i
        out.append(record)
    return out


def write_result(row: int, status: str, error: str | None = None) -> None:
    with _lock:
        wb = _open_or_create()
        ws = _worksheet(wb)
        header = [cell.value for cell in ws[1]]
        col = {name: i + 1 for i, name in enumerate(header)}

        sent_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if status == "sent" else ""
        ws.cell(row=row, column=col["Status"], value=status)
        ws.cell(row=row, column=col["Sent Time"], value=sent_time)
        ws.cell(row=row, column=col["Error"], value=error or "")
        wb.save(_path())


def get_lead_counts() -> dict:
    counts = {"total": 0, "sent": 0, "failed": 0, "pending": 0}
    for record in read_recipients(skip_already_sent=False):
        status = str(record.get("Status") or "").strip().lower()
        counts["total"] += 1
        counts[status if status in ("sent", "failed") else "pending"] += 1
    return counts
