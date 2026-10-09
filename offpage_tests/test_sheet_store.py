"""sheet_store.SheetStore against a fake gspread.Worksheet — covers both
roles it plays: the scraper's RowSink, and outreach's read/write-status."""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from sheet_store import HEADERS, SheetStore


class FakeWorksheet:
    """Minimal gspread.Worksheet stand-in: a plain 2D list of strings, like
    what the real API returns, with the handful of methods SheetStore uses."""

    def __init__(self):
        self.rows: list[list[str]] = []

    def row_values(self, n):
        return self.rows[n - 1] if len(self.rows) >= n else []

    def update(self, range_name, values):
        while len(self.rows) < 1:
            self.rows.append([])
        self.rows[0] = list(values[0])

    def append_rows(self, rows, value_input_option="RAW"):
        for row in rows:
            self.rows.append([str(v) for v in row])

    def get_all_records(self):
        if not self.rows:
            return []
        header = self.rows[0]
        return [dict(zip(header, row)) for row in self.rows[1:]]

    def update_cell(self, row, col, value):
        while len(self.rows) < row:
            self.rows.append([""] * len(self.rows[0]) if self.rows else [])
        r = self.rows[row - 1]
        while len(r) < col:
            r.append("")
        r[col - 1] = value

    def clear(self):
        self.rows = []


class SheetStoreTests(unittest.TestCase):
    def test_ensure_headers_writes_them_once(self):
        ws = FakeWorksheet()
        store = SheetStore(ws)
        store.ensure_headers()
        self.assertEqual(ws.rows[0], list(HEADERS))

        store.ensure_headers()  # calling again must not duplicate/clear
        self.assertEqual(len(ws.rows), 1)

    def test_append_then_read_round_trips(self):
        ws = FakeWorksheet()
        store = SheetStore(ws)
        store.ensure_headers()
        store.append_rows([
            ["01-Jan-2026", "widgets", "q", "a.test", "https://a.test",
             "lead@a.test", "", "snippet", "50", "", "", ""],
        ])
        leads = store.read_recipients(skip_already_sent=False)
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["Email"], "lead@a.test")
        self.assertEqual(leads[0]["row"], 2)

    def test_write_result_and_lead_counts(self):
        ws = FakeWorksheet()
        store = SheetStore(ws)
        store.ensure_headers()
        store.append_rows([
            ["d", "k", "q", "a.test", "https://a.test", "a@a.test", "", "", "0", "", "", ""],
            ["d", "k", "q", "b.test", "https://b.test", "b@b.test", "", "", "0", "", "", ""],
        ])
        store.write_result(2, status="sent")
        store.write_result(3, status="failed", error="boom")

        counts = store.get_lead_counts()
        self.assertEqual(counts, {"total": 2, "sent": 1, "failed": 1, "pending": 0})

        # skip_already_sent only skips "sent" rows — a "failed" one is still
        # eligible for a retry, same as local_store/sheets_source.
        pending = store.read_recipients(skip_already_sent=True)
        self.assertEqual([r["Email"] for r in pending], ["b@b.test"])

    def test_clear_wipes_rows_but_keeps_the_header(self):
        ws = FakeWorksheet()
        store = SheetStore(ws)
        store.ensure_headers()
        store.append_rows([
            ["d", "k", "q", "a.test", "https://a.test", "a@a.test", "", "", "0", "", "", ""],
        ])

        store.clear()

        self.assertEqual(ws.rows, [list(HEADERS)])
        self.assertEqual(store.read_recipients(skip_already_sent=False), [])


if __name__ == "__main__":
    unittest.main()
