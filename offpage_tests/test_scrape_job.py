"""scrape_job's state machine, with the real Selenium/Sheets batch runner
substituted for a fake - these tests never open a browser."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db
import scrape_job


class ScrapeJobStateTests(unittest.TestCase):
    def setUp(self):
        # _start() logs every scrape to db.search_history (see
        # db.record_search_start) - isolated here the same way the other
        # db.py tests are, so this never touches the real tracking.db.
        db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "tracking.db"
        db.init_db()
        scrape_job._status = scrape_job.ScrapeStatus()
        scrape_job._stop_requested = False
        self._real_run_batch = scrape_job._run_batch

    def tearDown(self):
        scrape_job._run_batch = self._real_run_batch

    def _use_fake_batch(self, found: int, final_state: str = "awaiting_decision"):
        """Fake _run_batch: pretends to find `found` leads, then sets final_state directly."""
        def fake(keyword, username, is_owner, country=None, language=None, history_id=None):
            for i in range(1, found + 1):
                scrape_job._on_lead(i, type("L", (), {"domain": f"lead{i}.test"})())
            with scrape_job._lock:
                scrape_job._status.state = final_state
        scrape_job._run_batch = fake

    def test_start_refuses_while_already_running(self):
        self._use_fake_batch(found=0)
        scrape_job._status.state = "running"  # simulate already busy
        self.assertFalse(scrape_job.start("pept", "alice", False))

    def test_start_then_wait_reaches_awaiting_decision_with_counts(self):
        self._use_fake_batch(found=5)
        self.assertTrue(scrape_job.start("pept", "alice", False))
        self._wait_until_not_running()
        status = scrape_job.get_status()
        self.assertEqual(status["state"], "awaiting_decision")
        self.assertEqual(status["queued_this_batch"], 5)
        self.assertEqual(status["queued_total"], 5)
        self.assertEqual(status["keyword"], "pept")
        self.assertEqual(status["username"], "alice")

    def test_continue_scraping_accumulates_total_but_resets_batch_count(self):
        self._use_fake_batch(found=5)
        scrape_job.start("pept", "alice", False)
        self._wait_until_not_running()

        self._use_fake_batch(found=3)
        self.assertTrue(scrape_job.continue_scraping())
        self._wait_until_not_running()

        status = scrape_job.get_status()
        self.assertEqual(status["queued_this_batch"], 3)
        self.assertEqual(status["queued_total"], 8)  # 5 + 3, accumulated

    def test_continue_scraping_refuses_when_not_awaiting_decision(self):
        scrape_job._status.state = "idle"
        self.assertFalse(scrape_job.continue_scraping())

    def test_zero_new_leads_ends_in_finished_not_awaiting_decision(self):
        self._use_fake_batch(found=0, final_state="finished")
        scrape_job.start("pept", "alice", False)
        self._wait_until_not_running()
        self.assertEqual(scrape_job.get_status()["state"], "finished")

    def test_two_different_users_running_a_scrape_get_their_own_username_recorded(self):
        self._use_fake_batch(found=1)
        scrape_job.start("pept", "bob", True)
        self._wait_until_not_running()
        self.assertEqual(scrape_job.get_status()["username"], "bob")

    def test_start_with_no_region_defaults_to_the_united_kingdom(self):
        self._use_fake_batch(found=1)
        scrape_job.start("pept", "alice", False)
        self._wait_until_not_running()
        status = scrape_job.get_status()
        self.assertEqual(status["country"], "uk")
        self.assertEqual(status["language"], "en-GB")

    def test_an_unrecognized_region_falls_back_to_the_united_kingdom(self):
        self._use_fake_batch(found=1)
        scrape_job.start("pept", "alice", False, country="not-a-real-country", language="not-a-real-language")
        self._wait_until_not_running()
        status = scrape_job.get_status()
        self.assertEqual(status["country"], "uk")
        self.assertEqual(status["language"], "en-GB")

    def test_a_chosen_region_is_recorded_and_carried_into_continue_scraping(self):
        self._use_fake_batch(found=5)
        scrape_job.start("pept", "alice", False, country="us", language="en-US")
        self._wait_until_not_running()
        status = scrape_job.get_status()
        self.assertEqual(status["country"], "us")
        self.assertEqual(status["language"], "en-US")

        received = {}

        def fake_continue(keyword, username, is_owner, country=None, language=None, history_id=None):
            received["country"] = country
            received["language"] = language
            with scrape_job._lock:
                scrape_job._status.state = "finished"
        scrape_job._run_batch = fake_continue

        self.assertTrue(scrape_job.continue_scraping())
        self._wait_until_not_running()
        self.assertEqual(received["country"], "us")
        self.assertEqual(received["language"], "en-US")

    def test_the_actual_search_url_targets_the_chosen_country_and_language(self):
        """End-to-end check (no fake) that a chosen region really changes
        the URL the scraper would open - this is what makes the dropdown
        actually affect the search, not just get recorded and ignored."""
        from offpage.regions import home_url
        self.assertEqual(
            home_url("us", "en-US"), "https://www.google.com/?hl=en-US&gl=us&pws=0",
        )
        self.assertEqual(
            home_url("uk", "en-GB"), "https://www.google.com/?hl=en-GB&gl=uk&pws=0",
        )
        self.assertNotEqual(home_url("us", "en-US"), home_url("de", "de"))

    def test_start_refuses_while_a_previous_batch_is_awaiting_decision(self):
        """A fresh start() must not silently discard a paused batch - that
        batch's keyword/counts would just vanish with no way back to it."""
        self._use_fake_batch(found=5)
        scrape_job.start("pept", "alice", False)
        self._wait_until_not_running()
        self.assertEqual(scrape_job.get_status()["state"], "awaiting_decision")

        self.assertFalse(scrape_job.start("other-keyword", "alice", False))
        self.assertEqual(scrape_job.get_status()["keyword"], "pept")  # untouched

    def test_dismiss_decision_clears_a_paused_batch_back_to_idle(self):
        self._use_fake_batch(found=5)
        scrape_job.start("pept", "alice", False)
        self._wait_until_not_running()

        self.assertTrue(scrape_job.dismiss_decision())
        status = scrape_job.get_status()
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["keyword"], "")

        self.assertTrue(scrape_job.start("fresh-keyword", "alice", False))  # no longer blocked

    def test_dismiss_decision_is_a_no_op_when_nothing_is_paused(self):
        scrape_job._status.state = "idle"
        self.assertFalse(scrape_job.dismiss_decision())

    def _wait_until_not_running(self, timeout: float = 2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if scrape_job.get_status()["state"] != "running":
                return
            time.sleep(0.01)
        self.fail("fake batch never finished")


if __name__ == "__main__":
    unittest.main()
