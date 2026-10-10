"""app.py's /forgot-password and /reset-password/<token> routes, end to
end through Flask's test_client. _send_password_reset_email is mocked out
so this never opens a real SMTP connection; the sent flash message is
identical whether or not the username/recovery-email exists, so a
stranger probing the form can't use it to enumerate accounts.

db.DB_PATH is pointed at a throwaway file BEFORE app.py is ever imported
(app.py runs db.init_db()/_seed_owner_account() at import time, and that
must never land on the real tracking.db) and again in setUp() for every
test (the Flask `app` object is a singleton - only created once no matter
how many test modules import it - so a module-level DB_PATH would get
silently overwritten by whichever test module happens to import app.py
last; setUp() re-pointing it per test is what actually isolates tests
from each other, regardless of import order)."""
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


class ForgotPasswordRouteTests(unittest.TestCase):
    def setUp(self):
        db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "tracking.db"
        db.init_db()
        db.create_user("alice", "oldhash", role="member", created_by="owner")
        db.save_manual_email_account("alice", "alice@gmail.com", "app-pass")
        db.create_user("noemail", "oldhash", role="member", created_by="owner")  # never finished onboarding
        db.create_user("oauthuser", "oldhash", role="member", created_by="owner")
        db.save_google_account("oauthuser", "oauthuser@gmail.com", "refresh", "access", "sheet-1")

    def test_a_known_user_with_a_connected_email_gets_a_token_emailed(self):
        client = app_module.app.test_client()
        with mock.patch.object(app_module, "_send_password_reset_email") as send_mail:
            resp = client.post("/forgot-password", data={"username": "alice"}, follow_redirects=False)

        self.assertEqual(resp.status_code, 302)
        send_mail.assert_called_once()
        to_email, reset_url = send_mail.call_args[0]
        self.assertEqual(to_email, "alice@gmail.com")
        self.assertIn("/reset-password/", reset_url)

    def test_an_unknown_username_sends_no_email_but_gives_the_same_response(self):
        client = app_module.app.test_client()
        with mock.patch.object(app_module, "_send_password_reset_email") as send_mail:
            resp = client.post("/forgot-password", data={"username": "nobody"}, follow_redirects=False)

        self.assertEqual(resp.status_code, 302)
        send_mail.assert_not_called()

    def test_a_user_with_no_connected_email_yet_sends_no_email_but_doesnt_error(self):
        client = app_module.app.test_client()
        with mock.patch.object(app_module, "_send_password_reset_email") as send_mail:
            resp = client.post("/forgot-password", data={"username": "noemail"}, follow_redirects=False)

        self.assertEqual(resp.status_code, 302)
        send_mail.assert_not_called()

    def test_a_valid_token_for_a_manual_gmail_account_resets_the_login_password_only(self):
        """The login password and the Gmail App Password are separate
        secrets now (see signup()) - resetting one must never touch the
        other, for an account connected either way."""
        token = db.create_password_reset_token("alice")
        client = app_module.app.test_client()

        resp = client.post(
            f"/reset-password/{token}",
            data={"password": "brandnewpass123", "confirm_password": "brandnewpass123"},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(db.get_valid_password_reset_token(token))  # single-use
        from werkzeug.security import check_password_hash
        self.assertTrue(check_password_hash(db.get_user_by_username("alice")["password_hash"], "brandnewpass123"))
        self.assertEqual(db.get_manual_email_account("alice")["app_password"], "app-pass")  # untouched

    def test_mismatched_confirmation_is_rejected_without_consuming_the_token(self):
        token = db.create_password_reset_token("alice")
        client = app_module.app.test_client()

        resp = client.post(
            f"/reset-password/{token}",
            data={"password": "brandnewpass123", "confirm_password": "something-else"},
        )

        self.assertEqual(resp.status_code, 200)  # re-rendered with an error, not redirected
        self.assertIsNotNone(db.get_valid_password_reset_token(token))  # not consumed

    def test_a_valid_token_for_an_oauth_only_account_also_resets_the_login_password(self):
        token = db.create_password_reset_token("oauthuser")
        client = app_module.app.test_client()

        resp = client.post(
            f"/reset-password/{token}",
            data={"password": "brandnewpass123", "confirm_password": "brandnewpass123"},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 302)
        from werkzeug.security import check_password_hash
        self.assertTrue(check_password_hash(db.get_user_by_username("oauthuser")["password_hash"], "brandnewpass123"))

    def test_a_bogus_token_is_rejected_without_touching_any_password(self):
        client = app_module.app.test_client()
        resp = client.get("/reset-password/not-a-real-token", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/forgot-password", resp.headers["Location"])


if __name__ == "__main__":
    unittest.main()
