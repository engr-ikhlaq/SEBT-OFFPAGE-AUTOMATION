"""db.get/set_leads_watermark — backs the dashboard/Compose "Clear leads"
button. Clearing must only ever raise a per-user watermark in tracking.db;
it must never touch the actual Sheet/local file (see app.py's clear_leads
and _visible_leads, which filter rows against this watermark)."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import db


class LeadsWatermarkTests(unittest.TestCase):
    def setUp(self):
        db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "tracking.db"
        db.init_db()

    def test_defaults_to_zero_for_a_user_who_never_cleared(self):
        self.assertEqual(db.get_leads_watermark("alice"), 0)

    def test_set_then_get_round_trips(self):
        db.set_leads_watermark("alice", 12)
        self.assertEqual(db.get_leads_watermark("alice"), 12)

    def test_setting_again_replaces_rather_than_stacks(self):
        db.set_leads_watermark("alice", 12)
        db.set_leads_watermark("alice", 30)
        self.assertEqual(db.get_leads_watermark("alice"), 30)

    def test_two_users_watermarks_are_independent(self):
        db.set_leads_watermark("alice", 12)
        db.set_leads_watermark("bob", 99)
        self.assertEqual(db.get_leads_watermark("alice"), 12)
        self.assertEqual(db.get_leads_watermark("bob"), 99)


if __name__ == "__main__":
    unittest.main()
