"""google_oauth.py — the parts testable without a real Google consent
screen: config detection, rebuilding Credentials from a stored row, and
get_or_create_sheet()'s create-vs-reuse logic against a fake gspread client.

Not tested here (needs a live Google account): authorization_url() /
exchange_code() actually round-tripping through Google's servers, and
get_connected_email()'s real HTTP call.
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import gspread
import google_oauth


class IsConfiguredTests(unittest.TestCase):
    def test_false_when_env_vars_are_missing(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(google_oauth.is_configured())

    def test_true_when_both_are_set(self):
        env = {"GOOGLE_OAUTH_CLIENT_ID": "id", "GOOGLE_OAUTH_CLIENT_SECRET": "secret"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertTrue(google_oauth.is_configured())

    def test_client_config_raises_a_clear_error_when_not_configured(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(google_oauth.NotConfigured):
                google_oauth._client_config()


class CredentialsFromRowTests(unittest.TestCase):
    def test_rebuilds_without_hitting_the_network_when_not_expired(self):
        env = {"GOOGLE_OAUTH_CLIENT_ID": "id", "GOOGLE_OAUTH_CLIENT_SECRET": "secret"}
        row = {"access_token": "access-1", "refresh_token": "refresh-1"}
        with mock.patch.dict("os.environ", env, clear=True):
            creds = google_oauth.credentials_from_row(row)
        self.assertEqual(creds.token, "access-1")
        self.assertEqual(creds.refresh_token, "refresh-1")
        self.assertEqual(set(creds.scopes), set(google_oauth.SCOPES))


class FakeSpreadsheet:
    def __init__(self, id_):
        self.id = id_
        self.sheet1 = mock.Mock()


class FakeGspreadClient:
    """Stands in for gspread.Client — .open() raises SpreadsheetNotFound
    unless .create() has already been called for that title."""

    def __init__(self):
        self._by_title: dict[str, FakeSpreadsheet] = {}
        self.create_calls = 0

    def open(self, title):
        if title not in self._by_title:
            raise gspread.SpreadsheetNotFound(title)
        return self._by_title[title]

    def create(self, title):
        self.create_calls += 1
        sh = FakeSpreadsheet(id_=f"sheet-id-{self.create_calls}")
        self._by_title[title] = sh
        return sh


class GetOrCreateSheetTests(unittest.TestCase):
    def test_creates_a_sheet_with_headers_the_first_time(self):
        client = FakeGspreadClient()
        sheet_id = google_oauth.get_or_create_sheet(credentials=None, client=client)
        self.assertEqual(sheet_id, "sheet-id-1")
        self.assertEqual(client.create_calls, 1)
        header_call = client._by_title[google_oauth.SHEET_TITLE].sheet1.update
        header_call.assert_called_once_with(
            range_name="A1:F1", values=[list(google_oauth.SHEET_HEADERS)]
        )

    def test_reuses_the_existing_sheet_on_a_second_call(self):
        client = FakeGspreadClient()
        first = google_oauth.get_or_create_sheet(credentials=None, client=client)
        second = google_oauth.get_or_create_sheet(credentials=None, client=client)
        self.assertEqual(first, second)
        self.assertEqual(client.create_calls, 1)  # not created twice


if __name__ == "__main__":
    unittest.main()
