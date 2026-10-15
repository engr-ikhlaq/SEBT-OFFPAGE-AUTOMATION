"""db.py's search_history table - the admin "who searched for what" log
(see app.py's /admin/search-history and scrape_job.py's _start()/_run_batch())."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import db


class SearchHistoryDbTests(unittest.TestCase):
    def setUp(self):
        db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "tracking.db"
        db.init_db()

    def test_a_started_search_is_recorded_as_running_with_no_leads_yet(self):
        history_id = db.record_search_start("alice", "pept", "uk", "en-GB")
        row = db.get_search_history()[0]
        self.assertEqual(row["id"], history_id)
        self.assertEqual(row["username"], "alice")
        self.assertEqual(row["keyword"], "pept")
        self.assertEqual(row["country"], "uk")
        self.assertEqual(row["language"], "en-GB")
        self.assertEqual(row["final_state"], "running")
        self.assertEqual(row["leads_found"], 0)
        self.assertIsNone(row["finished_at"])

    def test_finishing_a_search_updates_its_own_row_only(self):
        other_id = db.record_search_start("bob", "other", "us", "en-US")
        history_id = db.record_search_start("alice", "pept", "uk", "en-GB")

        db.record_search_finish(history_id, leads_found=7, final_state="finished")

        by_id = {row["id"]: row for row in db.get_search_history()}
        self.assertEqual(by_id[history_id]["leads_found"], 7)
        self.assertEqual(by_id[history_id]["final_state"], "finished")
        self.assertIsNotNone(by_id[history_id]["finished_at"])
        # The other user's still-running search is untouched.
        self.assertEqual(by_id[other_id]["final_state"], "running")
        self.assertEqual(by_id[other_id]["leads_found"], 0)

    def test_history_is_returned_newest_first(self):
        db.record_search_start("alice", "first", "uk", "en-GB")
        db.record_search_start("alice", "second", "uk", "en-GB")
        keywords = [row["keyword"] for row in db.get_search_history()]
        self.assertEqual(keywords, ["second", "first"])


if __name__ == "__main__":
    unittest.main()
