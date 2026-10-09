"""reply_checker.py's mailbox resolution — the actual bug behind "Check
replies doesn't track replies": it used to only ever check the static
IMAP_HOST in .env, even when a user's outreach was sent from their own
connected Gmail (a completely different mailbox). _imap_config_for_sender
and check_for_replies' bucketing are what fixed that; these tests cover
both without ever opening a real IMAP connection (imaplib is mocked)."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import db
import reply_checker


class ImapConfigForSenderTests(unittest.TestCase):
    def setUp(self):
        db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "tracking.db"
        db.init_db()
        db.create_user("ikhlaq-wahid", "hash", role="owner", created_by="setup")
        db.save_manual_email_account("ikhlaq-wahid", "mairablogwrites@gmail.com", "app-pass-123")

        db.create_user("oauthuser", "hash", role="member", created_by="owner")
        db.save_google_account("oauthuser", "oauthuser@gmail.com", "refresh", "access", "sheet-1")

        self._env = mock.patch.dict("os.environ", {
            "IMAP_HOST": "imap.gmail.com", "IMAP_PORT": "993",
            "IMAP_USERNAME": "shared@gmail.com", "IMAP_PASSWORD": "shared-pass",
        }, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_a_manually_connected_senders_mailbox_uses_their_own_app_password(self):
        cfg = reply_checker._imap_config_for_sender("mairablogwrites@gmail.com")
        self.assertEqual(cfg, {
            "host": "imap.gmail.com", "port": 993,
            "username": "mairablogwrites@gmail.com", "password": "app-pass-123",
        })

    def test_an_oauth_only_senders_mailbox_is_skipped_not_guessed(self):
        self.assertIsNone(reply_checker._imap_config_for_sender("oauthuser@gmail.com"))

    def test_the_shared_identity_falls_back_to_the_static_env_config(self):
        cfg = reply_checker._imap_config_for_sender("shared@gmail.com")
        self.assertEqual(cfg["username"], "shared@gmail.com")
        self.assertEqual(cfg["password"], "shared-pass")

    def test_no_sender_email_on_record_falls_back_to_the_static_env_config(self):
        cfg = reply_checker._imap_config_for_sender(None)
        self.assertEqual(cfg["username"], "shared@gmail.com")

    def test_lookup_is_case_insensitive(self):
        cfg = reply_checker._imap_config_for_sender("MairaBlogWrites@Gmail.com")
        self.assertEqual(cfg["username"], "mairablogwrites@gmail.com")


class CheckForRepliesBucketingTests(unittest.TestCase):
    """check_for_replies() must scan each DISTINCT resolved mailbox once,
    never the sender's actual mailbox under the wrong credentials, and
    skip anything it has no credential for — without raising."""

    def setUp(self):
        db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "tracking.db"
        db.init_db()
        db.create_user("ikhlaq-wahid", "hash", role="owner", created_by="setup")
        db.save_manual_email_account("ikhlaq-wahid", "mairablogwrites@gmail.com", "app-pass-123")
        db.create_user("oauthuser", "hash", role="member", created_by="owner")
        db.save_google_account("oauthuser", "oauthuser@gmail.com", "refresh", "access", "sheet-1")

        self._env = mock.patch.dict("os.environ", {"IMAP_HOST": "imap.gmail.com", "IMAP_USERNAME": "shared@gmail.com",
                                                     "IMAP_PASSWORD": "shared-pass"}, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)

        campaign_id = db.create_campaign(subject="s", message="m", link_url="", link_text="")
        self.campaign_id = campaign_id

    def _send(self, email, sender_email):
        _, token = db.create_recipient(self.campaign_id, email, sender_email=sender_email)
        db.mark_recipient_result(email, self.campaign_id, "sent", message_id=f"<{token}@test>")

    def test_recipients_from_different_mailboxes_are_scanned_separately(self):
        self._send("a@x.test", sender_email="mairablogwrites@gmail.com")
        self._send("b@x.test", sender_email="shared@gmail.com")
        self._send("c@x.test", sender_email="oauthuser@gmail.com")  # no IMAP credential — must be skipped

        with mock.patch.object(reply_checker, "_scan_mailbox", return_value=0) as scan:
            reply_checker.check_for_replies()

        self.assertEqual(scan.call_count, 2)  # mairablogwrites's mailbox + the shared fallback — not oauthuser's
        scanned_usernames = {call.args[0]["username"] for call in scan.call_args_list}
        self.assertEqual(scanned_usernames, {"mairablogwrites@gmail.com", "shared@gmail.com"})

    def test_two_recipients_sharing_a_mailbox_are_scanned_together_once(self):
        self._send("a@x.test", sender_email="mairablogwrites@gmail.com")
        self._send("b@x.test", sender_email="mairablogwrites@gmail.com")

        with mock.patch.object(reply_checker, "_scan_mailbox", return_value=0) as scan:
            reply_checker.check_for_replies()

        self.assertEqual(scan.call_count, 1)
        self.assertEqual(len(scan.call_args_list[0].args[1]), 2)  # both recipients, one IMAP login

    def test_no_pending_recipients_is_a_safe_no_op(self):
        self.assertEqual(reply_checker.check_for_replies(), 0)


if __name__ == "__main__":
    unittest.main()
