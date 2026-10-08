"""Settings defaults and validation."""
from __future__ import annotations

import unittest

from offpage.config import ConfigError, Settings


class SettingsDefaultsTests(unittest.TestCase):
    def test_default_batch_size_writes_one_lead_at_a_time(self):
        self.assertEqual(Settings(spreadsheet_id="x").batch_size, 1)

    def test_default_crawl_workers_is_positive(self):
        self.assertGreaterEqual(Settings(spreadsheet_id="x").crawl_workers, 1)


class SettingsValidationTests(unittest.TestCase):
    def test_rejects_missing_spreadsheet_id(self):
        with self.assertRaises(ConfigError):
            Settings(spreadsheet_id="")

    def test_rejects_non_positive_batch_size(self):
        with self.assertRaises(ConfigError):
            Settings(spreadsheet_id="x", batch_size=0)

    def test_rejects_non_positive_crawl_workers(self):
        with self.assertRaises(ConfigError):
            Settings(spreadsheet_id="x", crawl_workers=0)


if __name__ == "__main__":
    unittest.main()
