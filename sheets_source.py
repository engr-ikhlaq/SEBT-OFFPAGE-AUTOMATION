"""
sheets_source.py
Google Sheets integration for the mailer.

Expected sheet layout (header row required, any column order is fine
as long as the header text matches exactly):

    Email | Keyword | Domain | Status | Sent Time | Error

- Email, Keyword, Domain are inputs you fill in yourself.
- Status, Sent Time, Error are written back by this app after each send
  attempt, so you get a live log directly in the sheet.

Setup required (see README / chat walkthrough):
    1. Enable the Google Sheets API + Google Drive API in a Google Cloud project.
    2. Create a SERVICE ACCOUNT (not an OAuth "Desktop" client), download its JSON key.
    3. Share the target sheet with the service account's client_email (Editor access).
    4. Set these in your .env:
         GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json
         GOOGLE_SHEET_ID=1AbCdEfGhIjKlMnOpQrStUvWxYz...
         GOOGLE_WORKSHEET_NAME=Sheet1
"""

import os
from datetime import datetime, timezone
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

REQUIRED_HEADERS = ["Email", "Keyword", "Domain", "Status", "Sent Time", "Error"]


def is_configured() -> bool:
    """True if the .env has enough info to attempt a Sheets connection."""
    return bool(
        os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
        and os.environ.get("GOOGLE_SHEET_ID")
    )


def _get_worksheet():
    creds_file = os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"]
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    worksheet_name = os.environ.get("GOOGLE_WORKSHEET_NAME", "Sheet1")

    if not os.path.exists(creds_file):
        raise FileNotFoundError(
            f"GOOGLE_SERVICE_ACCOUNT_FILE is set to '{creds_file}' but that file "
            f"doesn't exist. Download the service account JSON key and place it there."
        )

    creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(sheet_id)
    return sheet.worksheet(worksheet_name)


def read_recipients(skip_already_sent: bool = True) -> list[dict]:
    """
    Reads all rows from the sheet and returns a list of dicts, e.g.:
        [{"row": 2, "Email": "a@example.com", "Keyword": "...", "Domain": "...",
          "Status": "", "Sent Time": "", "Error": ""}, ...]

    `row` is the actual sheet row number (1-indexed, header is row 1),
    needed later to write status back to the correct row.

    If skip_already_sent is True, rows whose Status is already "sent"
    are excluded — handy for re-running a partially-failed batch without
    re-emailing people who already got the message.
    """
    ws = _get_worksheet()
    records = ws.get_all_records()  # list of dicts keyed by header row

    header = ws.row_values(1)
    missing = [h for h in REQUIRED_HEADERS if h not in header]
    if missing:
        raise ValueError(
            f"Sheet is missing required column(s): {', '.join(missing)}. "
            f"Found headers: {header}"
        )

    recipients = []
    for i, record in enumerate(records, start=2):  # row 1 is header
        email = str(record.get("Email", "")).strip()
        if not email:
            continue
        if skip_already_sent and str(record.get("Status", "")).strip().lower() == "sent":
            continue
        record["row"] = i
        recipients.append(record)

    return recipients


def write_result(row: int, status: str, error: Optional[str] = None):
    """
    Writes Status / Sent Time / Error back to a specific row after a send
    attempt. `row` is the sheet row number returned by read_recipients().
    """
    ws = _get_worksheet()
    header = ws.row_values(1)

    col_status = header.index("Status") + 1
    col_sent_time = header.index("Sent Time") + 1
    col_error = header.index("Error") + 1

    sent_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if status == "sent" else ""

    ws.update_cell(row, col_status, status)
    ws.update_cell(row, col_sent_time, sent_time)
    ws.update_cell(row, col_error, error or "")
