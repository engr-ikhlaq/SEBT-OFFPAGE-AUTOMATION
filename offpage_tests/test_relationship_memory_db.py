"""db.py's relationship memory (get_contacted_domains) and self-learning
reply-rate signal (get_tld_reply_rates) - see /admin/relationships and
scrape_job.py's use of get_tld_reply_rates() when building each Scorer."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import db


def _make_campaign(username="alice"):
    return db.create_campaign("subj", "msg", None, None, username=username)


def _make_outreach_recipient(campaign_id, email, domain, sent=True, replied=False, kind="outreach"):
    recipient_id, _token = db.create_recipient(campaign_id, email, kind=kind, lead_domain=domain)
    if sent:
        db.mark_recipient_result(email, campaign_id, status="sent")
    if replied:
        db.record_reply(recipient_id)
    return recipient_id


class ContactedDomainsTests(unittest.TestCase):
    def setUp(self):
        db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "tracking.db"
        db.init_db()

    def test_a_domain_never_contacted_is_absent(self):
        self.assertEqual(db.get_contacted_domains(), {})

    def test_the_most_recent_contact_per_domain_is_kept(self):
        campaign = _make_campaign("alice")
        _make_outreach_recipient(campaign, "old@site.test", "site.test")
        _make_outreach_recipient(campaign, "new@site.test", "site.test")

        history = db.get_contacted_domains()
        self.assertEqual(history["site.test"]["email"], "new@site.test")
        self.assertEqual(history["site.test"]["username"], "alice")

    def test_a_campaign_row_with_no_domain_is_ignored(self):
        campaign = _make_campaign("alice")
        db.create_recipient(campaign, "a@b.test", kind="campaign")  # no lead_domain at all
        self.assertEqual(db.get_contacted_domains(), {})


class TldReplyRatesTests(unittest.TestCase):
    def setUp(self):
        db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "tracking.db"
        db.init_db()

    def test_a_suffix_below_the_minimum_sample_is_left_out(self):
        campaign = _make_campaign()
        for i in range(4):  # below MIN_TLD_SAMPLE (5)
            _make_outreach_recipient(campaign, f"a{i}@site{i}.com", f"site{i}.com", replied=True)
        self.assertEqual(db.get_tld_reply_rates(), {})

    def test_a_suffix_at_the_minimum_sample_gets_a_real_rate(self):
        campaign = _make_campaign()
        for i in range(4):
            _make_outreach_recipient(campaign, f"a{i}@site{i}.com", f"site{i}.com", replied=True)
        _make_outreach_recipient(campaign, "a4@site4.com", "site4.com", replied=False)

        rates = db.get_tld_reply_rates()
        self.assertEqual(rates[".com"], 0.8)  # 4 replied of 5 sent

    def test_only_sent_outreach_rows_count_not_pending_or_campaign_mail(self):
        campaign = _make_campaign()
        for i in range(5):
            _make_outreach_recipient(campaign, f"a{i}@site{i}.org", f"site{i}.org", replied=True)
        # A pending (never sent) row and a plain campaign-kind row shouldn't count.
        _make_outreach_recipient(campaign, "pending@site9.org", "site9.org", sent=False)
        _make_outreach_recipient(campaign, "camp@site9.org", "site9.org", kind="campaign")

        self.assertEqual(db.get_tld_reply_rates()[".org"], 1.0)  # still just the 5 real sends


if __name__ == "__main__":
    unittest.main()
