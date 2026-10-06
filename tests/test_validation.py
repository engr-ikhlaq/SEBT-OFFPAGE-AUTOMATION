"""Tests for email/domain validation and the relevance gate."""
from __future__ import annotations

import unittest

import dns.exception

from offpage.models import PageData
from offpage.parsing import extract_emails
from offpage.relevance import RelevanceGate, keyword_terms
from offpage.scoring import Scorer
from offpage.validation import EmailValidator, is_plausible_domain, is_valid_email_syntax


class FakeResolver:
    """Stands in for dns.resolver.Resolver. Domains in `mx` answer; others raise."""

    def __init__(self, mx: set[str]) -> None:
        self._mx = mx
        self.lifetime = 5.0
        self.calls: list[str] = []

    def resolve(self, domain, rdtype):
        self.calls.append(domain)
        if domain in self._mx:
            return ["mx.example"]
        raise dns.exception.DNSException("no MX")


class EmailSyntaxTests(unittest.TestCase):
    def test_accepts_normal_addresses(self):
        for good in ("info@blog.com", "first.last+tag@sub.blog.co.uk"):
            self.assertTrue(is_valid_email_syntax(good), good)

    def test_rejects_malformed_addresses(self):
        for bad in ("info@", "@example.com", "a..b@example.com", "info@example", "info@ex ample.com", "info@.com"):
            self.assertFalse(is_valid_email_syntax(bad), bad)

    def test_rejects_asset_names_placeholders_and_no_reply(self):
        for bad in ("logo@2x.png", "you@example.com", "x@sentry.io", "noreply@site.com", "a@cdn.wixpress.com"):
            self.assertFalse(is_valid_email_syntax(bad), bad)

    def test_extract_emails_only_returns_valid_syntax(self):
        text = "Write to Info@Blog.com, not logo@2x.png or no-reply@blog.com"
        self.assertEqual(extract_emails(text), frozenset({"info@blog.com"}))


class DomainTests(unittest.TestCase):
    def test_plausible_domains(self):
        self.assertTrue(is_plausible_domain("scientificaminos.com"))
        self.assertTrue(is_plausible_domain("www.example.co.uk"))

    def test_rejects_numbers_and_junk(self):
        for bad in ("1", "4", "50+", "60+", "example", "bad_domain.com", ""):
            self.assertFalse(is_plausible_domain(bad), bad)


class EmailValidatorTests(unittest.TestCase):
    def test_keeps_only_syntax_valid_and_mx_backed_addresses(self):
        validator = EmailValidator(resolver=FakeResolver(mx={"blog.com"}))
        verified = validator.filter_valid(
            ["Info@Blog.com", "info@blog.com", "x@nomail.org", "logo@2x.png", "bad@"]
        )
        self.assertEqual(verified, ["info@blog.com"])

    def test_mx_lookup_is_cached_per_domain(self):
        resolver = FakeResolver(mx={"blog.com"})
        validator = EmailValidator(resolver=resolver)
        validator.filter_valid(["a@blog.com", "b@blog.com"])
        self.assertEqual(resolver.calls, ["blog.com"])


class RelevanceGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = RelevanceGate(Scorer())

    def test_keyword_terms_drop_short_and_stop_words(self):
        self.assertEqual(keyword_terms("snapchat cloaking agency"), ("snapchat", "cloaking", "agency"))
        self.assertEqual(keyword_terms("pept"), ("pept",))

    def test_relevant_guest_post_page_passes(self):
        page = PageData(
            title="Peptide research guest posts",
            body="We welcome guest posts about peptide research. Write for us.",
        )
        self.assertIsNone(self.gate.rejection_reason("pept", page))

    def test_off_topic_page_is_rejected(self):
        page = PageData(title="Cheap flights", body="Write for us if you love travel guest posts.")
        self.assertIn("topic terms", self.gate.rejection_reason("pept", page))

    def test_on_topic_page_without_guest_post_wording_is_rejected(self):
        page = PageData(title="Peptide products", body="Buy peptide supplements today.")
        self.assertIn("guest-post", self.gate.rejection_reason("pept", page))

    def test_every_term_must_appear(self):
        page = PageData(title="Snapchat tips", body="Write for us: guest post about snapchat.")
        self.assertIn("cloaking", self.gate.rejection_reason("snapchat cloaking agency", page))


if __name__ == "__main__":
    unittest.main()
