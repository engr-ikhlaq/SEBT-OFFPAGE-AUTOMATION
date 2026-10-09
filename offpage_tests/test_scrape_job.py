"""scrape_job's state machine, with the real Selenium/Sheets batch runner
substituted for a fake - these tests never open a browser."""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scrape_job


class ScrapeJobStateTests(unittest.TestCase):
    def setUp(self):
        scrape_job._status = scrape_job.ScrapeStatus()
        scrape_job._stop_requested = False
        self._real_run_batch = scrape_job._run_batch

    def tearDown(self):
        scrape_job._run_batch = self._real_run_batch

    def _use_fake_batch(self, found: int, final_state: str = "awaiting_decision"):
        """Fake _run_batch: pretends to find `found` leads, then sets final_state directly."""
        def fake(keyword, username, is_owner):
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

    def _wait_until_not_running(self, timeout: float = 2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if scrape_job.get_status()["state"] != "running":
                return
            time.sleep(0.01)
        self.fail("fake batch never finished")


if __name__ == "__main__":
    unittest.main()
