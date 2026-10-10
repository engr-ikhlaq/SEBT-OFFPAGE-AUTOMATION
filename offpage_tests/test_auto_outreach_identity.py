"""auto_outreach.py sends through whatever (cfg, sender) the caller passes
in - no fixed persona of its own. sheets_source/db are faked so this never
touches the real sheet or tracking.db, and _render_email_html is stubbed
since the real one needs a Flask app context (render_template)."""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import auto_outreach
from mailer import MailerConfig


class FakeSheetsSource:
    def __init__(self, leads):
        self._leads = leads
        self.is_configured = lambda: True
        self.written: list[tuple] = []

    def read_recipients(self, skip_already_sent=True):
        return self._leads

    def write_result(self, row, status, error=None):
        self.written.append((row, status, error))


class FakeDb:
    def __init__(self):
        self.campaigns = 0
        self.recipient_results: list[tuple] = []

    def create_campaign(self, **kwargs):
        self.campaigns += 1
        return self.campaigns

    def create_recipient(self, campaign_id, email, **kwargs):
        return (1, f"token-{email}")

    def mark_recipient_result(self, email, campaign_id, status, **kwargs):
        self.recipient_results.append((email, status))

    def get_recipients_due_for_followup(self, username, hours):
        return [{
            "id": 1, "campaign_id": 1, "email": "lead@site.test",
            "message_id": "<orig@mail>", "sent_at": "...",
            "lead_domain": "site.test", "lead_url": "https://site.test",
        }]

    def mark_followed_up(self, recipient_id):
        pass


class RecordingSender:
    """Captures which identity (cfg) and how many messages went through it."""

    def __init__(self):
        self.enter_count = 0
        self.sent_as: list[str] = []

    def __enter__(self):
        self.enter_count += 1
        return self

    def __exit__(self, *exc):
        return False

    def send(self, msg):
        self.sent_as.append(msg["From"])


def _cfg(name: str) -> MailerConfig:
    return MailerConfig(host="", port=0, username=f"{name}@x.test", password="",
                         from_name=name, from_email=f"{name}@x.test", reply_to=f"{name}@x.test",
                         send_delay_seconds=0)


class OutreachUsesInjectedIdentityTests(unittest.TestCase):
    def setUp(self):
        self._real_db = auto_outreach.db
        self._real_render = auto_outreach._render_email_html
        auto_outreach._render_email_html = lambda body_html, **kw: f"<html>{body_html}</html>"

    def tearDown(self):
        auto_outreach.db = self._real_db
        auto_outreach._render_email_html = self._real_render

    def test_send_pending_leads_sends_as_the_passed_in_identity(self):
        backend = FakeSheetsSource([
            {"Email": "lead@site.test", "Domain": "site.test", "URL": "https://site.test/page",
             "Keyword": "widgets", "row": 2},
        ])
        auto_outreach.db = FakeDb()

        cfg = _cfg("alice")
        sender = RecordingSender()
        result = auto_outreach.send_pending_leads("https://app.test", cfg, sender, backend, "alice")

        self.assertEqual(result["sent"], ["lead@site.test"])
        self.assertEqual(sender.enter_count, 1)
        self.assertIn("alice", sender.sent_as[0])  # From header carries the identity we passed in
        self.assertEqual(backend.written, [(2, "sent", None)])

    def test_a_different_caller_gets_a_different_identity_and_backend_with_no_shared_state(self):
        leads = [{"Email": "lead@site.test", "Domain": "site.test", "URL": "https://site.test",
                  "Keyword": "widgets", "row": 2}]
        auto_outreach.db = FakeDb()
        backend_a = FakeSheetsSource(leads)
        sender_a = RecordingSender()
        auto_outreach.send_pending_leads("https://app.test", _cfg("alice"), sender_a, backend_a, "alice")

        auto_outreach.db = FakeDb()
        backend_b = FakeSheetsSource(leads)
        sender_b = RecordingSender()
        auto_outreach.send_pending_leads("https://app.test", _cfg("bob"), sender_b, backend_b, "bob")

        self.assertIn("alice", sender_a.sent_as[0])
        self.assertIn("bob", sender_b.sent_as[0])
        # Each call only ever wrote to its OWN backend - the point of the
        # whole per-user change: Alice's run never touches Bob's data.
        self.assertEqual(backend_a.written, [(2, "sent", None)])
        self.assertEqual(backend_b.written, [(2, "sent", None)])

    def test_send_due_followups_also_uses_the_passed_in_identity(self):
        auto_outreach.db = FakeDb()
        sender = RecordingSender()
        result = auto_outreach.send_due_followups("https://app.test", _cfg("alice"), sender, "alice")

        self.assertEqual(result["sent"], ["lead@site.test"])
        self.assertIn("alice", sender.sent_as[0])


if __name__ == "__main__":
    unittest.main()
