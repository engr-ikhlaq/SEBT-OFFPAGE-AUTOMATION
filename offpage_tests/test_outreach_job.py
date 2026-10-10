"""outreach_job's state machine, with the real send functions substituted
for a fake - these tests never open an SMTP connection."""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import outreach_job


class OutreachJobStateTests(unittest.TestCase):
    def setUp(self):
        outreach_job._status = outreach_job.OutreachStatus()
        outreach_job._stop_requested = False
        self._real_send_pending_leads = outreach_job.auto_outreach.send_pending_leads
        self._real_send_due_followups = outreach_job.auto_outreach.send_due_followups

    def tearDown(self):
        outreach_job.auto_outreach.send_pending_leads = self._real_send_pending_leads
        outreach_job.auto_outreach.send_due_followups = self._real_send_due_followups

    def _use_fake_send(self, total: int, sent: int):
        """Fake send_pending_leads: reports progress for `total` leads,
        the first `sent` as sent and the rest as failed."""
        def fake(base_url, cfg, sender, backend, username, on_progress=None, should_stop=None, limit=None):
            for i in range(1, total + 1):
                if should_stop is not None and should_stop():
                    break
                if on_progress is not None:
                    on_progress(i, total, f"lead{i}@x.test", "sent" if i <= sent else "failed")
            return {"sent": [f"lead{i}@x.test" for i in range(1, sent + 1)],
                     "failed": [(f"lead{i}@x.test", "boom") for i in range(sent + 1, total + 1)],
                     "skipped": []}
        outreach_job.auto_outreach.send_pending_leads = fake

    def test_start_refuses_while_already_running(self):
        self._use_fake_send(total=0, sent=0)
        outreach_job._status.state = "running"
        self.assertFalse(outreach_job.start("https://x", None, None, None, "alice"))

    def test_start_then_wait_reaches_finished_with_counts(self):
        self._use_fake_send(total=5, sent=3)
        self.assertTrue(outreach_job.start("https://x", None, None, None, "alice"))
        self._wait_until_not_running()
        status = outreach_job.get_status()
        self.assertEqual(status["state"], "finished")
        self.assertEqual(status["total"], 5)
        self.assertEqual(status["done"], 5)
        self.assertEqual(status["sent_count"], 3)
        self.assertEqual(status["failed_count"], 2)
        self.assertEqual(status["kind"], "outreach")

    def test_stop_is_honored_mid_run(self):
        import threading

        item1_done = threading.Event()
        go_ahead = threading.Event()

        def fake(base_url, cfg, sender, backend, username, on_progress=None, should_stop=None, limit=None):
            for i in range(1, 11):
                if should_stop is not None and should_stop():
                    break
                if on_progress is not None:
                    on_progress(i, 10, f"lead{i}@x.test", "sent")
                if i == 1:
                    item1_done.set()
                    go_ahead.wait(timeout=2)  # pauses here so the test can call stop() deterministically
            return {"sent": [], "failed": [], "skipped": []}
        outreach_job.auto_outreach.send_pending_leads = fake

        self.assertTrue(outreach_job.start("https://x", None, None, None, "alice"))
        self.assertTrue(item1_done.wait(timeout=2))
        outreach_job.stop()
        go_ahead.set()  # fake resumes, sees should_stop() == True now, and breaks
        self._wait_until_not_running()

        status = outreach_job.get_status()
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(status["done"], 1)  # exactly one item processed before stopping

    def test_an_exception_is_reported_as_error_not_left_hanging(self):
        def fake(base_url, cfg, sender, backend, username, on_progress=None, should_stop=None, limit=None):
            raise RuntimeError("smtp exploded")
        outreach_job.auto_outreach.send_pending_leads = fake

        outreach_job.start("https://x", None, None, None, "alice")
        self._wait_until_not_running()
        status = outreach_job.get_status()
        self.assertEqual(status["state"], "error")
        self.assertIn("smtp exploded", status["error"])

    def test_followups_run_reports_finished_with_counts(self):
        def fake(base_url, cfg, sender, username, hours=24):
            return {"sent": ["a@x.test", "b@x.test"], "failed": [("c@x.test", "boom")]}
        outreach_job.auto_outreach.send_due_followups = fake

        self.assertTrue(outreach_job.start_followups("https://x", None, None, "alice"))
        self._wait_until_not_running()
        status = outreach_job.get_status()
        self.assertEqual(status["state"], "finished")
        self.assertEqual(status["kind"], "followups")
        self.assertEqual(status["sent_count"], 2)
        self.assertEqual(status["failed_count"], 1)

    def test_elapsed_seconds_is_reported_while_running(self):
        import threading

        release = threading.Event()

        def fake(base_url, cfg, sender, backend, username, on_progress=None, should_stop=None, limit=None):
            release.wait(timeout=2)
            return {"sent": [], "failed": [], "skipped": []}
        outreach_job.auto_outreach.send_pending_leads = fake

        outreach_job.start("https://x", None, None, None, "alice")
        time.sleep(0.05)
        status = outreach_job.get_status()
        self.assertEqual(status["state"], "running")
        self.assertGreaterEqual(status["elapsed_seconds"], 0)
        release.set()
        self._wait_until_not_running()

    def _wait_until_not_running(self, timeout: float = 2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if outreach_job.get_status()["state"] != "running":
                return
            time.sleep(0.01)
        self.fail("fake run never finished")


if __name__ == "__main__":
    unittest.main()
