"""db.py's google_accounts table (connected Google accounts, one per app user)."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import db


class GoogleAccountsDbTests(unittest.TestCase):
    def setUp(self):
        db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "tracking.db"
        db.init_db()

    def test_round_trip(self):
        self.assertIsNone(db.get_google_account("alice"))
        db.save_google_account("alice", "alice@gmail.com", "refresh-1", "access-1", "sheet-1")
        row = db.get_google_account("alice")
        self.assertEqual(row["google_email"], "alice@gmail.com")
        self.assertEqual(row["refresh_token"], "refresh-1")
        self.assertEqual(row["sheet_id"], "sheet-1")

    def test_connecting_again_replaces_the_row_not_duplicates_it(self):
        db.save_google_account("alice", "alice@gmail.com", "refresh-1", "access-1", "sheet-1")
        db.save_google_account("alice", "alice@gmail.com", "refresh-2", "access-2", "sheet-1")
        row = db.get_google_account("alice")
        self.assertEqual(row["refresh_token"], "refresh-2")

    def test_update_access_token_leaves_refresh_token_untouched(self):
        db.save_google_account("alice", "alice@gmail.com", "refresh-1", "access-1", "sheet-1")
        db.update_google_access_token("alice", "access-2")
        row = db.get_google_account("alice")
        self.assertEqual(row["access_token"], "access-2")
        self.assertEqual(row["refresh_token"], "refresh-1")

    def test_delete(self):
        db.save_google_account("alice", "alice@gmail.com", "refresh-1", "access-1", None)
        db.delete_google_account("alice")
        self.assertIsNone(db.get_google_account("alice"))

    def test_deleting_a_user_also_removes_their_connected_google_account(self):
        user_id = db.create_user("alice", "hash", role="owner")
        db.create_user("bob", "hash", role="member")  # so alice isn't the last user
        db.save_google_account("alice", "alice@gmail.com", "r1", "a1", "s1")

        self.assertTrue(db.delete_user(user_id))
        self.assertIsNone(db.get_google_account("alice"))

    def test_two_users_connect_independently(self):
        db.save_google_account("alice", "alice@gmail.com", "r1", "a1", "s1")
        db.save_google_account("bob", "bob@gmail.com", "r2", "a2", "s2")
        self.assertEqual(db.get_google_account("alice")["google_email"], "alice@gmail.com")
        self.assertEqual(db.get_google_account("bob")["google_email"], "bob@gmail.com")


if __name__ == "__main__":
    unittest.main()
