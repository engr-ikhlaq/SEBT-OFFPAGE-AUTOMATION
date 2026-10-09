"""db's password-reset-token functions — backs app.py's self-service
/forgot-password and /reset-password/<token>. A token must be single-use,
expire, and updating a password must actually take effect on login."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import db


class PasswordResetTokenTests(unittest.TestCase):
    def setUp(self):
        db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "tracking.db"
        db.init_db()
        db.create_user("alice", "oldhash", role="member", created_by="owner")

    def test_a_fresh_token_is_valid(self):
        token = db.create_password_reset_token("alice")
        row = db.get_valid_password_reset_token(token)
        self.assertIsNotNone(row)
        self.assertEqual(row["username"], "alice")

    def test_unknown_token_is_invalid(self):
        self.assertIsNone(db.get_valid_password_reset_token("not-a-real-token"))

    def test_a_used_token_is_no_longer_valid(self):
        token = db.create_password_reset_token("alice")
        db.mark_password_reset_token_used(token)
        self.assertIsNone(db.get_valid_password_reset_token(token))

    def test_an_expired_token_is_invalid(self):
        token = db.create_password_reset_token("alice", ttl_hours=0)
        time.sleep(0.01)  # ttl_hours=0 means "already expired"
        self.assertIsNone(db.get_valid_password_reset_token(token))

    def test_update_user_password_changes_the_stored_hash(self):
        db.update_user_password("alice", "newhash")
        self.assertEqual(db.get_user_by_username("alice")["password_hash"], "newhash")

    def test_creating_a_new_token_sweeps_out_expired_ones(self):
        expired = db.create_password_reset_token("alice", ttl_hours=0)
        time.sleep(0.01)
        db.create_password_reset_token("alice")  # triggers the sweep
        self.assertIsNone(db.get_valid_password_reset_token(expired))


class FindUsernameByConnectedEmailTests(unittest.TestCase):
    """Backs /signup's duplicate check — the same Gmail must not end up
    connected to two different app accounts, even when the existing
    account's username isn't that email (an older, manually-created one)."""

    def setUp(self):
        db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "tracking.db"
        db.init_db()
        db.create_user("ikhlaq-wahid", "hash", role="owner", created_by="setup")

    def test_finds_the_owner_of_a_manually_connected_email(self):
        db.save_manual_email_account("ikhlaq-wahid", "mairablogwrites@gmail.com", "app-pass")
        self.assertEqual(db.find_username_by_connected_email("mairablogwrites@gmail.com"), "ikhlaq-wahid")

    def test_finds_the_owner_of_an_oauth_connected_email(self):
        db.save_google_account("ikhlaq-wahid", "ikhlaq@gmail.com", "refresh", "access", "sheet-1")
        self.assertEqual(db.find_username_by_connected_email("ikhlaq@gmail.com"), "ikhlaq-wahid")

    def test_an_unclaimed_email_returns_none(self):
        self.assertIsNone(db.find_username_by_connected_email("nobody@gmail.com"))


if __name__ == "__main__":
    unittest.main()
