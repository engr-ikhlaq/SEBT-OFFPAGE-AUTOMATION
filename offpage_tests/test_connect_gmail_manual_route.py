"""app.py's /onboarding and /connect/gmail-manual - manually connecting a
Gmail address + App Password. Both now verify the credentials live
BEFORE saving (see _verify_gmail_app_password), closing a real gap: a
mistyped credential (someone's normal Gmail password instead of a real
16-character App Password, say) used to be accepted silently and only
fail later, confusingly, the first time something tried to actually
send - see db.save_manual_email_account's sender_name parameter for the
unrelated bug found alongside this one.

_verify_gmail_app_password is mocked so this never opens a real SMTP
connection."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import db

db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "tracking.db"
db.init_db()
import app as app_module

app_module.app.config["WTF_CSRF_ENABLED"] = False


def _client_logged_in_as(username: str):
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["username"] = username
        sess["role"] = "member"
    return client


_FORM_DATA = {"email": "mairablogwrites@gmail.com", "app_password": "wrong-password", "sender_name": "Maira"}


class OnboardingManualConnectTests(unittest.TestCase):
    def setUp(self):
        db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "tracking.db"
        db.init_db()
        db.create_user("alice", "hash", role="member", created_by="owner")

    def test_bad_credentials_are_rejected_and_never_saved(self):
        client = _client_logged_in_as("alice")
        with mock.patch.object(app_module, "_verify_gmail_app_password", side_effect=Exception("bad credentials")):
            resp = client.post("/onboarding", data=_FORM_DATA, follow_redirects=False)

        self.assertEqual(resp.status_code, 200)  # re-renders onboarding, not a redirect
        self.assertIsNone(db.get_manual_email_account("alice"))

    def test_valid_credentials_are_saved_with_the_sender_name(self):
        client = _client_logged_in_as("alice")
        with mock.patch.object(app_module, "_verify_gmail_app_password") as verify:
            resp = client.post("/onboarding", data=_FORM_DATA, follow_redirects=False)

        verify.assert_called_once_with("mairablogwrites@gmail.com", "wrong-password")
        self.assertEqual(resp.status_code, 302)
        account = db.get_manual_email_account("alice")
        self.assertEqual(account["email"], "mairablogwrites@gmail.com")
        self.assertEqual(account["sender_name"], "Maira")


class ConnectGmailManualRouteTests(unittest.TestCase):
    def setUp(self):
        db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "tracking.db"
        db.init_db()
        db.create_user("alice", "hash", role="member", created_by="owner")

    def test_bad_credentials_are_rejected_and_never_saved(self):
        client = _client_logged_in_as("alice")
        with mock.patch.object(app_module, "_verify_gmail_app_password", side_effect=Exception("bad credentials")):
            client.post("/connect/gmail-manual", data=_FORM_DATA, follow_redirects=False)

        self.assertIsNone(db.get_manual_email_account("alice"))

    def test_valid_credentials_are_saved(self):
        client = _client_logged_in_as("alice")
        with mock.patch.object(app_module, "_verify_gmail_app_password"):
            client.post("/connect/gmail-manual", data=_FORM_DATA, follow_redirects=False)

        account = db.get_manual_email_account("alice")
        self.assertEqual(account["email"], "mairablogwrites@gmail.com")
        self.assertEqual(account["sender_name"], "Maira")


if __name__ == "__main__":
    unittest.main()
