"""app.py's /signup route - self-service account creation: a Gmail
address + App Password is verified live and connected as the account's
sending identity, while full_name and a separately-chosen password (typed
twice, must match) set up the account itself. _verify_gmail_app_password
is mocked so this never opens a real SMTP connection.

db.DB_PATH is pointed at a throwaway file BEFORE app.py is ever imported,
and again in setUp() for every test - see test_forgot_password_route.py's
module docstring for why both matter."""
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

_FORM_DEFAULTS = {
    "full_name": "New Person",
    "email": "newperson@gmail.com",
    "app_password": "abcd efgh ijkl mnop",
    "password": "a-login-password",
    "confirm_password": "a-login-password",
}


def _signup_data(**overrides) -> dict:
    data = dict(_FORM_DEFAULTS)
    data.update(overrides)
    return data


class SignupRouteTests(unittest.TestCase):
    def setUp(self):
        db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "tracking.db"
        db.init_db()
        db.create_user("existing", "hash", role="member", created_by="owner")
        db.save_manual_email_account("existing", "existing@gmail.com", "old-pass")

    def test_valid_credentials_create_an_account_and_log_the_user_in(self):
        client = app_module.app.test_client()
        with mock.patch.object(app_module, "_verify_gmail_app_password") as verify:
            resp = client.post("/signup", data=_signup_data(email="NewPerson@Gmail.com"), follow_redirects=False)

        verify.assert_called_once_with("newperson@gmail.com", "abcd efgh ijkl mnop")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/")

        user = db.get_user_by_username("newperson@gmail.com")
        self.assertIsNotNone(user)
        self.assertEqual(user["role"], "member")
        self.assertEqual(user["full_name"], "New Person")

        manual = db.get_manual_email_account("newperson@gmail.com")
        self.assertEqual(manual["email"], "newperson@gmail.com")
        self.assertEqual(manual["app_password"], "abcd efgh ijkl mnop")

    def test_login_uses_the_chosen_password_not_the_app_password(self):
        client = app_module.app.test_client()
        with mock.patch.object(app_module, "_verify_gmail_app_password"):
            client.post("/signup", data=_signup_data())

        # The App Password must NOT work as a login password - the two are
        # deliberately separate secrets now. A rejected login re-renders
        # the form (200); only a successful one redirects (302).
        wrong = client.post(
            "/login", data={"username": "newperson@gmail.com", "password": "abcd efgh ijkl mnop"},
        )
        self.assertEqual(wrong.status_code, 200)

        right = client.post(
            "/login", data={"username": "newperson@gmail.com", "password": "a-login-password"},
            follow_redirects=False,
        )
        self.assertEqual(right.status_code, 302)
        self.assertEqual(right.headers["Location"], "/")  # straight to Compose, no onboarding needed

    def test_mismatched_password_confirmation_creates_no_account(self):
        client = app_module.app.test_client()
        with mock.patch.object(app_module, "_verify_gmail_app_password") as verify:
            resp = client.post("/signup", data=_signup_data(confirm_password="something-else"))

        verify.assert_not_called()
        self.assertEqual(resp.status_code, 200)  # re-rendered with an error
        self.assertIsNone(db.get_user_by_username("newperson@gmail.com"))

    def test_credentials_that_fail_live_verification_create_no_account(self):
        client = app_module.app.test_client()
        with mock.patch.object(app_module, "_verify_gmail_app_password", side_effect=Exception("bad credentials")):
            resp = client.post("/signup", data=_signup_data(email="nope@gmail.com"))

        self.assertEqual(resp.status_code, 200)  # re-rendered with an error
        self.assertIsNone(db.get_user_by_username("nope@gmail.com"))

    def test_an_already_registered_email_is_sent_to_login_instead(self):
        client = app_module.app.test_client()
        with mock.patch.object(app_module, "_verify_gmail_app_password") as verify:
            resp = client.post("/signup", data=_signup_data(email="existing@gmail.com"), follow_redirects=False)

        verify.assert_not_called()  # never even attempted - no credentials leaked to a duplicate check
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])
        # The ORIGINAL account must be untouched by the duplicate attempt.
        self.assertEqual(db.get_manual_email_account("existing")["app_password"], "old-pass")

    def test_an_orphaned_connection_from_a_deleted_account_does_not_block_resignup(self):
        """If an account was removed but its manual_email_accounts row
        somehow survived (see db.purge_orphaned_connections), that email
        must not be permanently stuck on "already has an account" with no
        account left to log into."""
        db.save_manual_email_account("ghost@gmail.com", "ghost@gmail.com", "old-pass")
        self.assertIsNone(db.get_user_by_username("ghost@gmail.com"))  # no real account - just the orphan

        client = app_module.app.test_client()
        with mock.patch.object(app_module, "_verify_gmail_app_password") as verify:
            resp = client.post(
                "/signup", data=_signup_data(email="ghost@gmail.com", app_password="new-pass"),
                follow_redirects=False,
            )

        verify.assert_called_once_with("ghost@gmail.com", "new-pass")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/")
        self.assertIsNotNone(db.get_user_by_username("ghost@gmail.com"))
        self.assertEqual(db.get_manual_email_account("ghost@gmail.com")["app_password"], "new-pass")

    def test_an_already_logged_in_user_is_redirected_away_from_signup(self):
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["username"] = "existing"
            sess["role"] = "member"

        resp = client.get("/signup", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/")


if __name__ == "__main__":
    unittest.main()
