"""data_source.for_user() — the priority order that keeps a new user's
data from ever landing in the admin's shared Sheet by accident — and
migrate_to_sheet()."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import data_source
import db
import local_store


class ForUserBackendSelectionTests(unittest.TestCase):
    def setUp(self):
        db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "tracking.db"
        db.init_db()

    def test_brand_new_user_gets_their_own_local_file_even_with_admin_sheet_configured(self):
        with mock.patch.object(data_source, "admin_sheet_configured", return_value=True):
            backend = data_source.for_user("newperson", is_owner=False)
        self.assertEqual(backend._path, local_store.user_leads_path("newperson"))
        self.assertNotEqual(backend._path, local_store.default_path())

    def test_owner_without_their_own_oauth_connection_uses_the_admin_sheet(self):
        with mock.patch.object(data_source, "admin_sheet_configured", return_value=True):
            backend = data_source.for_user("ikhlaq-wahid", is_owner=True)
        self.assertEqual(backend.name, "the shared Google Sheet")

    def test_owner_also_falls_back_to_local_if_admin_sheet_not_configured(self):
        with mock.patch.object(data_source, "admin_sheet_configured", return_value=False):
            backend = data_source.for_user("ikhlaq-wahid", is_owner=True)
        self.assertEqual(backend._path, local_store.user_leads_path("ikhlaq-wahid"))

    def test_a_users_own_oauth_sheet_wins_even_for_the_owner(self):
        db.save_google_account("ikhlaq-wahid", "ikhlaq@gmail.com", "refresh", "access", "sheet-123")
        fake_ws = object()
        with mock.patch.object(data_source, "admin_sheet_configured", return_value=True), \
             mock.patch.object(data_source.google_oauth, "credentials_from_row", return_value="creds"), \
             mock.patch.object(data_source.google_oauth, "open_user_worksheet", return_value=fake_ws) as open_ws:
            backend = data_source.for_user("ikhlaq-wahid", is_owner=True)
        open_ws.assert_called_once_with("creds", "sheet-123")
        self.assertIn("ikhlaq@gmail.com", backend.name)

    def test_non_owner_with_their_own_oauth_sheet_uses_it_not_local(self):
        db.save_google_account("newperson", "newperson@gmail.com", "refresh", "access", "sheet-999")
        with mock.patch.object(data_source.google_oauth, "credentials_from_row", return_value="creds"), \
             mock.patch.object(data_source.google_oauth, "open_user_worksheet", return_value=object()):
            backend = data_source.for_user("newperson", is_owner=False)
        self.assertIn("newperson@gmail.com", backend.name)

    def test_two_different_non_owner_users_never_share_a_local_file(self):
        with mock.patch.object(data_source, "admin_sheet_configured", return_value=True):
            alice = data_source.for_user("alice", is_owner=False)
            bob = data_source.for_user("bob", is_owner=False)
        self.assertNotEqual(alice._path, bob._path)


class ClearTests(unittest.TestCase):
    """backend.clear() — the dashboard/compose "delete my leads" button.
    Every backend variant must support it, and it must only ever touch
    the ONE backend it was called on."""

    def setUp(self):
        self._tmpdir = pathlib.Path(tempfile.mkdtemp())

    def test_local_backend_clear_deletes_only_that_users_file(self):
        alice_path = self._tmpdir / "leads_alice.xlsx"
        bob_path = self._tmpdir / "leads_bob.xlsx"
        with mock.patch.object(local_store, "user_leads_path",
                                side_effect=lambda name: {"alice": alice_path, "bob": bob_path}[name]):
            alice = data_source.for_user("alice", is_owner=False)
            bob = data_source.for_user("bob", is_owner=False)
        local_store.ExcelSink(path=alice_path).append_rows([
            ["d", "k", "q", "a.test", "https://a.test", "a@a.test", "", "", 0, "", "", ""],
        ])
        local_store.ExcelSink(path=bob_path).append_rows([
            ["d", "k", "q", "b.test", "https://b.test", "b@b.test", "", "", 0, "", "", ""],
        ])

        alice.clear()

        self.assertFalse(alice_path.exists())
        self.assertTrue(bob_path.exists())  # clearing Alice's never touches Bob's

    def test_user_sheet_backend_clear_delegates_to_the_sheet_store(self):
        class FakeStore:
            def __init__(self):
                self.cleared = False

            def clear(self):
                self.cleared = True

        fake_store = FakeStore()
        backend = data_source._UserSheetBackend(fake_store, "your connected Sheet")
        backend.clear()
        self.assertTrue(fake_store.cleared)

    def test_admin_sheets_backend_clear_delegates_to_sheets_source(self):
        backend = data_source._SheetsSourceBackend("the shared Google Sheet")
        with mock.patch.object(data_source.sheets_source, "clear_leads") as clear_leads:
            backend.clear()
        clear_leads.assert_called_once()


class MigrateToSheetTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = pathlib.Path(tempfile.mkdtemp())
        self._local_path = self._tmpdir / "leads_alice.xlsx"
        self._patch = mock.patch.object(local_store, "user_leads_path", return_value=self._local_path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_moves_rows_into_the_sheet_and_clears_the_local_file(self):
        local_store.ExcelSink(path=self._local_path).append_rows([
            ["d", "k", "q", "a.test", "https://a.test", "lead@a.test", "", "", 0, "", "", ""],
        ])

        sink_calls = []

        class FakeSink:
            def ensure_headers(self):
                sink_calls.append("ensure_headers")

            def append_rows(self, rows):
                sink_calls.append(("append_rows", rows))

        class FakeBackend:
            def make_sink(self):
                return FakeSink()

        moved = data_source.migrate_to_sheet("alice", FakeBackend())

        self.assertEqual(moved, 1)
        self.assertEqual(sink_calls[0], "ensure_headers")
        self.assertEqual(sink_calls[1][0], "append_rows")
        self.assertEqual(sink_calls[1][1][0][5], "lead@a.test")  # Email column
        self.assertFalse(self._local_path.exists())  # cleared after migrating

    def test_no_local_file_is_a_safe_no_op(self):
        class FakeBackend:
            def make_sink(self):
                raise AssertionError("should not be called when there's nothing to migrate")

        moved = data_source.migrate_to_sheet("alice", FakeBackend())
        self.assertEqual(moved, 0)


if __name__ == "__main__":
    unittest.main()
