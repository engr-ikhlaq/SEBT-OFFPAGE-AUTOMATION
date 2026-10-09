"""db.delete_lead_journey — the "x" button on each row of the dashboard's
"Guest-post outreach" list. Must remove the recipient and its events, clean
up a now-empty single-lead campaign (auto_outreach.py makes one per lead),
and never touch a campaign that still has other recipients on it."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import db


class DeleteLeadJourneyTests(unittest.TestCase):
    def setUp(self):
        db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "tracking.db"
        db.init_db()

    def test_deletes_the_recipient_and_its_single_use_campaign(self):
        campaign_id = db.create_campaign(subject="s", message="m", link_url="", link_text="")
        recipient_id, token = db.create_recipient(campaign_id, "lead@x.test", kind="outreach")
        db.record_open(token)

        self.assertTrue(db.delete_lead_journey(recipient_id))

        self.assertIsNone(db.get_recipient_by_token(token))
        with db.get_conn() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) c FROM campaigns WHERE id=?", (campaign_id,)).fetchone()["c"], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) c FROM events WHERE recipient_id=?", (recipient_id,)).fetchone()["c"], 0)

    def test_only_removes_the_targeted_leads_campaign_when_siblings_remain(self):
        campaign_id = db.create_campaign(subject="s", message="m", link_url="", link_text="")
        a_id, _ = db.create_recipient(campaign_id, "a@x.test", kind="outreach")
        b_id, _ = db.create_recipient(campaign_id, "b@x.test", kind="outreach")

        self.assertTrue(db.delete_lead_journey(a_id))

        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) c FROM campaigns WHERE id=?", (campaign_id,)).fetchone()["c"], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) c FROM recipients WHERE id=?", (b_id,)).fetchone()["c"], 1)

    def test_an_unknown_recipient_id_returns_false(self):
        self.assertFalse(db.delete_lead_journey(999999))

    def test_does_not_remove_a_non_outreach_campaign_recipient(self):
        campaign_id = db.create_campaign(subject="s", message="m", link_url="", link_text="")
        recipient_id, _ = db.create_recipient(campaign_id, "lead@x.test", kind="campaign")

        self.assertFalse(db.delete_lead_journey(recipient_id))
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) c FROM recipients WHERE id=?", (recipient_id,)).fetchone()["c"], 1)


if __name__ == "__main__":
    unittest.main()
