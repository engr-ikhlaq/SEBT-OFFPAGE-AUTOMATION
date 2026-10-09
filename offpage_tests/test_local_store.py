"""local_store.py (the default Excel-backed lead store) and data_source.py
(the Sheet-vs-local chooser)."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import local_store


class ExcelSinkTests(unittest.TestCase):
    def setUp(self):
        self._tmp = pathlib.Path(tempfile.mkdtemp()) / "leads.xlsx"
        self._env = mock.patch.dict("os.environ", {"LOCAL_LEADS_FILE": str(self._tmp)})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_ensure_headers_creates_the_file_with_the_right_columns(self):
        local_store.ExcelSink().ensure_headers()
        self.assertTrue(self._tmp.exists())
        leads = local_store.read_recipients(skip_already_sent=False)
        self.assertEqual(leads, [])  # header only, no data rows yet

    def test_append_rows_then_read_recipients_round_trips(self):
        sink = local_store.ExcelSink()
        sink.ensure_headers()
        sink.append_rows([
            ["01-Jan-2026", "widgets", "widgets guest post", "a.test", "https://a.test",
             "lead@a.test", "", "snippet", 50, "", "", ""],
        ])
        leads = local_store.read_recipients(skip_already_sent=False)
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["Email"], "lead@a.test")
        self.assertEqual(leads[0]["Domain"], "a.test")
        self.assertEqual(leads[0]["row"], 2)

    def test_rows_with_no_email_are_skipped(self):
        sink = local_store.ExcelSink()
        sink.ensure_headers()
        sink.append_rows([["d", "k", "q", "x.test", "https://x.test", "", "", "", 0, "", "", ""]])
        self.assertEqual(local_store.read_recipients(skip_already_sent=False), [])

    def test_write_result_updates_status_sent_time_and_error(self):
        sink = local_store.ExcelSink()
        sink.ensure_headers()
        sink.append_rows([
            ["d", "k", "q", "a.test", "https://a.test", "lead@a.test", "", "", 0, "", "", ""],
        ])
        local_store.write_result(2, status="failed", error="boom")
        leads = local_store.read_recipients(skip_already_sent=False)
        self.assertEqual(leads[0]["Status"], "failed")
        self.assertEqual(leads[0]["Error"], "boom")
        self.assertEqual(leads[0]["Sent Time"], "")  # only set on success

    def test_skip_already_sent_excludes_sent_rows_but_keeps_failed(self):
        sink = local_store.ExcelSink()
        sink.ensure_headers()
        sink.append_rows([
            ["d", "k", "q", "a.test", "https://a.test", "sent@a.test", "", "", 0, "", "", ""],
            ["d", "k", "q", "b.test", "https://b.test", "failed@b.test", "", "", 0, "", "", ""],
        ])
        local_store.write_result(2, status="sent")
        local_store.write_result(3, status="failed", error="x")

        pending = local_store.read_recipients(skip_already_sent=True)
        self.assertEqual([r["Email"] for r in pending], ["failed@b.test"])

    def test_get_lead_counts(self):
        sink = local_store.ExcelSink()
        sink.ensure_headers()
        sink.append_rows([
            ["d", "k", "q", "a.test", "https://a.test", "a@a.test", "", "", 0, "", "", ""],
            ["d", "k", "q", "b.test", "https://b.test", "b@b.test", "", "", 0, "", "", ""],
            ["d", "k", "q", "c.test", "https://c.test", "c@c.test", "", "", 0, "", "", ""],
        ])
        local_store.write_result(2, status="sent")
        local_store.write_result(3, status="failed", error="x")
        # row 4 left pending

        self.assertEqual(local_store.get_lead_counts(), {"total": 3, "sent": 1, "failed": 1, "pending": 1})

    def test_file_persists_across_separate_sink_instances(self):
        local_store.ExcelSink().append_rows([
            ["d", "k", "q", "a.test", "https://a.test", "a@a.test", "", "", 0, "", "", ""],
        ])
        # A fresh ExcelSink (a new object, simulating a later process opening
        # the same file) sees the row the first one wrote.
        local_store.ExcelSink().append_rows([
            ["d", "k", "q", "b.test", "https://b.test", "b@b.test", "", "", 0, "", "", ""],
        ])
        self.assertEqual(len(local_store.read_recipients(skip_already_sent=False)), 2)


class UserPathTests(unittest.TestCase):
    def test_different_users_get_different_paths(self):
        self.assertNotEqual(local_store.user_leads_path("alice"), local_store.user_leads_path("bob"))

    def test_path_is_stable_for_the_same_user(self):
        self.assertEqual(local_store.user_leads_path("alice"), local_store.user_leads_path("alice"))

    def test_unsafe_characters_in_username_are_sanitized(self):
        path = local_store.user_leads_path("alice@example.com")
        self.assertNotIn("@", str(path))
        self.assertTrue(str(path).startswith("leads_alice"))


class PerUserFileIsolationTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = pathlib.Path(tempfile.mkdtemp())
        self._alice = self._tmpdir / "alice.xlsx"
        self._bob = self._tmpdir / "bob.xlsx"

    def test_two_users_files_never_mix(self):
        local_store.ExcelSink(path=self._alice).append_rows([
            ["d", "k", "q", "a.test", "https://a.test", "alice-lead@a.test", "", "", 0, "", "", ""],
        ])
        local_store.ExcelSink(path=self._bob).append_rows([
            ["d", "k", "q", "b.test", "https://b.test", "bob-lead@b.test", "", "", 0, "", "", ""],
        ])

        alice_leads = local_store.read_recipients(skip_already_sent=False, path=self._alice)
        bob_leads = local_store.read_recipients(skip_already_sent=False, path=self._bob)
        self.assertEqual([r["Email"] for r in alice_leads], ["alice-lead@a.test"])
        self.assertEqual([r["Email"] for r in bob_leads], ["bob-lead@b.test"])

    def test_read_all_rows_returns_plain_lists_for_migration(self):
        local_store.ExcelSink(path=self._alice).append_rows([
            ["01-Jan-2026", "widgets", "q", "a.test", "https://a.test", "lead@a.test", "", "snippet", 50, "", "", ""],
        ])
        rows = local_store.read_all_rows(self._alice)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][5], "lead@a.test")  # Email column
        self.assertIsInstance(rows[0], list)

    def test_clear_removes_the_file(self):
        local_store.ExcelSink(path=self._alice).ensure_headers()
        self.assertTrue(self._alice.exists())
        local_store.clear(self._alice)
        self.assertFalse(self._alice.exists())


if __name__ == "__main__":
    unittest.main()
