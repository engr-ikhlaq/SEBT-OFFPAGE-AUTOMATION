"""
local_store.py
A local Excel file as a lead store — no setup, no credentials, works the
moment the app starts. Used in two ways (see data_source.py):
  - the shared fallback at LOCAL_LEADS_FILE, when nothing user-specific
    applies
  - a per-user file (user_leads_path) for anyone who hasn't connected
    their own Sheet — kept completely separate from everyone else's data,
    including the admin's shared Sheet

Same column layout as the Google Sheet path (sheets_source.py / sheet_store.py),
so the two are interchangeable and a user's local data can be migrated
into a Sheet later (see data_source.migrate_to_sheet) without conversion.

ExcelSink implements the same two methods offpage.sheets.SheetSink does
(ensure_headers, append_rows) — the RowSink protocol offpage.writer.
LeadWriter already expects — so the scraper writes to this exactly the
way it writes to a real Sheet, with no changes to offpage/ itself.
"""

from __future__ import annotations

import os
import re
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
# read-modify-write below goes through one lock. One lock for every file
# is coarser than strictly necessary (two different users' files don't
# actually conflict), but simple and correct, and these are small, quick
# operations — not worth a per-path lock registry for this app's scale.
_lock = threading.Lock()


def default_path() -> Path:
    return Path(os.environ.get("LOCAL_LEADS_FILE", "leads.xlsx"))


def user_leads_path(username: str) -> Path:
    """Each user who hasn't connected their own Sheet gets their own file,
    completely separate from everyone else's — including the admin's
    shared Sheet and the LOCAL_LEADS_FILE default."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", username) or "user"
    return Path(f"leads_{safe}.xlsx")


def _open_or_create(path: Path) -> Workbook:
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
    through — see module docstring. Bind it to a specific user's file with
    ExcelSink(path=local_store.user_leads_path(username))."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_path()

    def ensure_headers(self) -> None:
        with _lock:
            _open_or_create(self._path)  # creates the file with headers if missing; otherwise a no-op

    def append_rows(self, rows: list[list]) -> None:
        with _lock:
            wb = _open_or_create(self._path)
            ws = _worksheet(wb)
            for row in rows:
                ws.append(row)
            wb.save(self._path)


def is_configured() -> bool:
    """Always True — this is the no-setup fallback, not an optional feature."""
    return True


def read_recipients(skip_already_sent: bool = True, path: Path | None = None) -> list[dict]:
    """Same shape as sheets_source.read_recipients: a list of row dicts
    (keyed by header) with a 'row' key giving the actual spreadsheet row
    number, for write_result() to target later."""
    path = path or default_path()
    with _lock:
        wb = _open_or_create(path)
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


def read_all_rows(path: Path | None = None) -> list[list]:
    """Every data row as a plain list (no header, no dict, no filtering) —
    for migrating a file's contents elsewhere (see
    data_source.migrate_to_sheet), where what's wanted is "everything,
    exactly as stored" rather than the filtered/keyed view read_recipients
    gives callers that are about to send mail."""
    path = path or default_path()
    with _lock:
        wb = _open_or_create(path)
        rows = list(_worksheet(wb).iter_rows(values_only=True, min_row=2))
    return [[("" if v is None else v) for v in row] for row in rows]


def write_result(row: int, status: str, error: str | None = None, path: Path | None = None) -> None:
    path = path or default_path()
    with _lock:
        wb = _open_or_create(path)
        ws = _worksheet(wb)
        header = [cell.value for cell in ws[1]]
        col = {name: i + 1 for i, name in enumerate(header)}

        sent_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if status == "sent" else ""
        ws.cell(row=row, column=col["Status"], value=status)
        ws.cell(row=row, column=col["Sent Time"], value=sent_time)
        ws.cell(row=row, column=col["Error"], value=error or "")
        wb.save(path)


def get_lead_counts(path: Path | None = None) -> dict:
    counts = {"total": 0, "sent": 0, "failed": 0, "pending": 0}
    for record in read_recipients(skip_already_sent=False, path=path):
        status = str(record.get("Status") or "").strip().lower()
        counts["total"] += 1
        counts[status if status in ("sent", "failed") else "pending"] += 1
    return counts


def clear(path: Path) -> None:
    """Deletes a local file outright — used after migrate_to_sheet() copies
    its contents elsewhere, so the data exists in exactly one place."""
    with _lock:
        path.unlink(missing_ok=True)
