"""offpage.writer.LeadWriter - synchronous (batch_size behavior, unchanged)
and async_writes=True (background-thread writes, used by scrape_job.py so
the scraper doesn't block on the Sheets API round trip for each lead)."""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from offpage.models import Lead
from offpage.writer import LeadWriter


def _lead(domain: str) -> Lead:
    return Lead(
        run_date="2026-01-01", keyword="k", query="q", domain=domain,
        url=f"https://{domain}", emails=(f"hi@{domain}",), contact_url="",
        snippet="", score=1,
    )


class FakeSink:
    def __init__(self, fail_on: set[str] | None = None, delay: float = 0.0):
        self.calls: list[list] = []
        self._fail_on = fail_on or set()
        self._delay = delay

    def append_rows(self, rows: list[list]) -> None:
        if self._delay:
            time.sleep(self._delay)
        domains = {row[3] for row in rows}  # Lead.to_row()[3] is the domain
        if domains & self._fail_on:
            raise RuntimeError(f"simulated failure for {domains & self._fail_on}")
        self.calls.append(rows)


class FakeStore:
    def __init__(self):
        self.seen: set[str] = set()
        self.mark_seen_threads: set[int] = set()

    def mark_seen(self, domains) -> None:
        self.mark_seen_threads.add(threading.get_ident())
        self.seen.update(domains)


class SyncLeadWriterTests(unittest.TestCase):
    def test_batch_size_one_writes_immediately(self):
        sink, store = FakeSink(), FakeStore()
        writer = LeadWriter(sink, store, batch_size=1)
        writer.add(_lead("a.test"))
        self.assertEqual(len(sink.calls), 1)
        self.assertIn("a.test", store.seen)
        self.assertEqual(writer.pending, 0)

    def test_batch_size_above_one_waits_to_flush(self):
        sink, store = FakeSink(), FakeStore()
        writer = LeadWriter(sink, store, batch_size=3)
        writer.add(_lead("a.test"))
        writer.add(_lead("b.test"))
        self.assertEqual(len(sink.calls), 0)
        self.assertEqual(writer.pending, 2)
        writer.add(_lead("c.test"))
        self.assertEqual(len(sink.calls), 1)
        self.assertEqual(writer.pending, 0)

    def test_wait_all_is_a_safe_no_op_when_not_async(self):
        writer = LeadWriter(FakeSink(), FakeStore(), batch_size=1)
        writer.wait_all()  # must not raise
        writer.close()  # must not raise


class AsyncLeadWriterTests(unittest.TestCase):
    def test_add_returns_before_the_write_completes(self):
        sink, store = FakeSink(delay=0.2), FakeStore()
        writer = LeadWriter(sink, store, batch_size=1, async_writes=True)
        started = time.monotonic()
        writer.add(_lead("slow.test"))
        self.assertLess(time.monotonic() - started, 0.1)  # didn't block on the 0.2s write
        self.assertEqual(len(sink.calls), 0)  # not landed yet either
        writer.wait_all()
        writer.close()
        self.assertEqual(len(sink.calls), 1)
        self.assertIn("slow.test", store.seen)

    def test_wait_all_blocks_until_every_queued_write_has_landed(self):
        sink, store = FakeSink(delay=0.05), FakeStore()
        writer = LeadWriter(sink, store, batch_size=1, async_writes=True)
        for i in range(5):
            writer.add(_lead(f"d{i}.test"))
        writer.wait_all()
        self.assertEqual(len(sink.calls), 5)
        self.assertEqual(len(store.seen), 5)
        writer.close()

    def test_a_failed_write_is_raised_from_wait_all_and_leaves_the_domain_unseen(self):
        sink, store = FakeSink(fail_on={"bad.test"}), FakeStore()
        writer = LeadWriter(sink, store, batch_size=1, async_writes=True)
        writer.add(_lead("good.test"))
        writer.add(_lead("bad.test"))
        with self.assertRaises(RuntimeError):
            writer.wait_all()
        self.assertIn("good.test", store.seen)
        self.assertNotIn("bad.test", store.seen)  # failed write - stays unseen, retried next run
        writer.close()

    def test_mark_seen_happens_on_the_background_thread_not_the_caller(self):
        sink, store = FakeSink(), FakeStore()
        writer = LeadWriter(sink, store, batch_size=1, async_writes=True)
        writer.add(_lead("a.test"))
        writer.wait_all()
        writer.close()
        self.assertNotIn(threading.get_ident(), store.mark_seen_threads)

    def test_close_stops_the_background_thread(self):
        writer = LeadWriter(FakeSink(), FakeStore(), batch_size=1, async_writes=True)
        worker = writer._worker
        writer.add(_lead("a.test"))
        writer.wait_all()
        writer.close()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
