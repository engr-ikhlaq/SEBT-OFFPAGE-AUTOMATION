"""offpage.storage.SeenStore - in particular that it's safe to call
mark_seen() from a different thread than the one that opened it, which
offpage.writer.LeadWriter's async_writes mode now does (see test_writer.py)."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from offpage.storage import SeenStore


class SeenStoreThreadSafetyTests(unittest.TestCase):
    def setUp(self):
        self._path = pathlib.Path(tempfile.mkdtemp()) / "seen.db"

    def test_mark_seen_from_a_different_thread_than_the_one_that_opened_it(self):
        with SeenStore(self._path) as store:
            error = []

            def worker():
                try:
                    store.mark_seen(["other-thread.test"])
                except Exception as exc:
                    error.append(exc)

            t = threading.Thread(target=worker)
            t.start()
            t.join(timeout=2)

            self.assertEqual(error, [])
            self.assertTrue(store.is_seen("other-thread.test"))

    def test_concurrent_mark_seen_calls_dont_corrupt_each_other(self):
        with SeenStore(self._path) as store:
            def worker(i):
                store.mark_seen([f"d{i}.test"])

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=2)

            for i in range(20):
                self.assertTrue(store.is_seen(f"d{i}.test"))

    def test_a_domain_already_marked_is_seen_after_reopening(self):
        with SeenStore(self._path) as store:
            store.mark_seen(["persisted.test"])
        with SeenStore(self._path) as store:
            self.assertTrue(store.is_seen("persisted.test"))
            self.assertFalse(store.is_seen("never-seen.test"))


if __name__ == "__main__":
    unittest.main()
