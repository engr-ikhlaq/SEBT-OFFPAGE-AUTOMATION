"""db.py's manual_email_accounts table (the no-OAuth-setup Gmail connect)."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import db


class ManualEmailAccountsDbTests(unittest.TestCase):
    def setUp(self):
        db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "tracking.db"
        db.init_db()

    def test_round_trip(self):
        self.assertIsNone(db.get_manual_email_account("alice"))
        db.save_manual_email_account("alice", "alice@gmail.com", "abcd efgh ijkl mnop")
        row = db.get_manual_email_account("alice")
        self.assertEqual(row["email"], "alice@gmail.com")
        self.assertEqual(row["app_password"], "abcd efgh ijkl mnop")

    def test_connecting_again_replaces_not_duplicates(self):
        db.save_manual_email_account("alice", "alice@gmail.com", "old-pass")
        db.save_manual_email_account("alice", "alice@gmail.com", "new-pass")
        self.assertEqual(db.get_manual_email_account("alice")["app_password"], "new-pass")

    def test_delete(self):
        db.save_manual_email_account("alice", "alice@gmail.com", "pass")
        db.delete_manual_email_account("alice")
        self.assertIsNone(db.get_manual_email_account("alice"))

    def test_independent_of_a_google_oauth_connection(self):
        db.save_google_account("alice", "alice@gmail.com", "refresh", "access", "sheet")
        db.save_manual_email_account("alice", "alice-work@gmail.com", "pass")
        # Both can coexist; app.py's sender_for_current_user() decides which wins.
        self.assertIsNotNone(db.get_google_account("alice"))
        self.assertIsNotNone(db.get_manual_email_account("alice"))

    def test_sender_name_defaults_to_none_until_set(self):
        db.save_manual_email_account("alice", "alice@gmail.com", "pass")
        self.assertIsNone(db.get_manual_email_account("alice")["sender_name"])

    def test_sender_name_can_be_given_at_connect_time(self):
        db.save_manual_email_account("alice", "alice@gmail.com", "pass", sender_name="Maira")
        self.assertEqual(db.get_manual_email_account("alice")["sender_name"], "Maira")

    def test_reconnecting_without_a_sender_name_keeps_the_existing_one(self):
        # A real scenario: updating just the app password shouldn't wipe out
        # a sender_name that was already set for this mailbox.
        db.save_manual_email_account("alice", "alice@gmail.com", "old-pass", sender_name="Maira")
        db.save_manual_email_account("alice", "alice@gmail.com", "new-pass")
        self.assertEqual(db.get_manual_email_account("alice")["sender_name"], "Maira")

    def test_set_sender_name_updates_an_existing_manual_connection(self):
        db.save_manual_email_account("alice", "alice@gmail.com", "pass")
        db.set_sender_name("alice", "Maira")
        self.assertEqual(db.get_manual_email_account("alice")["sender_name"], "Maira")


if __name__ == "__main__":
    unittest.main()
