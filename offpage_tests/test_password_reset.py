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


if __name__ == "__main__":
    unittest.main()
