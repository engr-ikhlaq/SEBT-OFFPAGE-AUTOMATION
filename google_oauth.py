"""
google_oauth.py
"Connect Google Account" — one click, one consent screen, and the logged-in
app user can send from their own Gmail (no app password) and gets a Sheet
of their own (no manual service-account sharing).

Setup this still needs from you (Google requires this; nothing here can
skip it): an OAuth Client ID registered in Google Cloud Console, with
GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET set in .env, and
"http://localhost:5000/connect/google/callback" (or your real domain)
added as an authorized redirect URI. See the README section this module
is documented alongside.

How it works:
1. /connect/google builds an authorization_url() and redirects the user
   to Google's own consent screen. They pick an account and approve;
   nothing here ever sees their Google password.
2. Google redirects back to /connect/google/callback with a one-time
   code. exchange_code() swaps it for OAuth credentials (an access token
   good for ~1 hour, and a refresh token that doesn't expire until
   revoked).
3. get_connected_email() asks Google which address was just connected.
4. get_or_create_sheet() makes (or reuses) a spreadsheet in that
   account's own Drive, with the same header row sheets_source.py
   expects — so "pull recipients from a connected Google Sheet" works
   immediately, with zero manual sharing steps.
5. db.save_google_account() stores it all, keyed by the app's own
   logged-in username (one connection per dashboard user — each person
   connects their own Gmail).

Scopes requested (shown to the user on Google's consent screen, not
hidden): send email as them, read/write spreadsheets, and create files
in their Drive (not read their whole Drive — just what this app makes).
"""

from __future__ import annotations

import os

import gspread
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
)

SHEET_TITLE = "MailFlow Leads"
SHEET_HEADERS = ("Email", "Keyword", "Domain", "Status", "Sent Time", "Error")


class NotConfigured(RuntimeError):
    """GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET aren't set."""


def is_configured() -> bool:
    return bool(os.environ.get("GOOGLE_OAUTH_CLIENT_ID") and os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET"))


def _client_config() -> dict:
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise NotConfigured(
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set in .env. "
            "See the 'Connect Google Account' section of the README for the one-time "
            "Google Cloud Console setup."
        )
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def build_flow(redirect_uri: str) -> Flow:
    return Flow.from_client_config(_client_config(), scopes=list(SCOPES), redirect_uri=redirect_uri)


def authorization_url(redirect_uri: str) -> tuple[str, str]:
    """Returns (url, state). Store `state` (e.g. in the session) and check
    it against the callback's `state` query param — the standard OAuth
    defense against a forged callback request."""
    flow = build_flow(redirect_uri)
    url, state = flow.authorization_url(
        access_type="offline",       # ask for a refresh token, not just a short-lived access token
        include_granted_scopes="true",
        prompt="consent",            # re-shows consent so a refresh token is issued even on reconnect
    )
    return url, state


def exchange_code(redirect_uri: str, authorization_response_url: str) -> Credentials:
    """Swaps the callback's one-time code for real credentials."""
    flow = build_flow(redirect_uri)
    flow.fetch_token(authorization_response=authorization_response_url)
    return flow.credentials


def credentials_from_row(row: dict) -> Credentials:
    """Rebuilds a Credentials object from what db.py stored."""
    client_id = os.environ["GOOGLE_OAUTH_CLIENT_ID"]
    client_secret = os.environ["GOOGLE_OAUTH_CLIENT_SECRET"]
    creds = Credentials(
        token=row["access_token"],
        refresh_token=row["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=list(SCOPES),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
    return creds


def get_connected_email(credentials: Credentials, http_session=None) -> str:
    import requests  # local import: only needed here, keeps module import light

    http = http_session or requests.Session()
    resp = http.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {credentials.token}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["email"]


def get_or_create_sheet(credentials: Credentials, client: gspread.Client | None = None) -> str:
    """Returns the spreadsheet id of this account's MailFlow sheet, creating
    it (with the right headers) the first time."""
    gc = client or gspread.Client(auth=credentials)
    try:
        sh = gc.open(SHEET_TITLE)
    except gspread.SpreadsheetNotFound:
        sh = gc.create(SHEET_TITLE)
        sh.sheet1.update(range_name="A1:F1", values=[list(SHEET_HEADERS)])
    return sh.id
