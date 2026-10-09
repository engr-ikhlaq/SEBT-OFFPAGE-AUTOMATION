"""mailer.py's pluggable Sender transports — SmtpSender and GmailApiSender —
against fakes, so no test opens a real socket."""
from __future__ import annotations

import smtplib
import sys
import unittest
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mailer


class FakeSMTP:
    """Stands in for smtplib.SMTP across the whole app."""

    sent: list[EmailMessage] = []
    login_calls: list[tuple[str, str]] = []

    def __init__(self, host, port):
        self.host, self.port = host, port

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, username, password):
        FakeSMTP.login_calls.append((username, password))

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)

    def quit(self):
        pass


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttpSession:
    def __init__(self):
        self.posts: list[dict] = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append({"url": url, "headers": headers, "json": json})
        return FakeResponse(200)


class FakeCredentials:
    def __init__(self, token="tok-abc", expired=False, refresh_token="refresh-xyz"):
        self.token = token
        self.expired = expired
        self.refresh_token = refresh_token
        self.refreshed = False

    def refresh(self, request):
        self.refreshed = True
        self.token = "tok-refreshed"
        self.expired = False


def _message() -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "hi"
    msg["From"] = "a@b.com"
    msg["To"] = "c@d.com"
    msg.set_content("hello")
    return msg


class SmtpSenderTests(unittest.TestCase):
    def setUp(self):
        FakeSMTP.sent = []
        FakeSMTP.login_calls = []
        self._real_smtp = smtplib.SMTP
        smtplib.SMTP = FakeSMTP

    def tearDown(self):
        smtplib.SMTP = self._real_smtp

    def test_logs_in_once_and_sends_through_the_same_connection(self):
        cfg = mailer.MailerConfig(host="h", port=587, username="u", password="p",
                                   from_name="N", from_email="n@h.com", reply_to="n@h.com")
        with mailer.SmtpSender(cfg) as sender:
            sender.send(_message())
            sender.send(_message())
        self.assertEqual(FakeSMTP.login_calls, [("u", "p")])
        self.assertEqual(len(FakeSMTP.sent), 2)


class GmailApiSenderTests(unittest.TestCase):
    def test_sends_as_a_bearer_authorized_post_with_base64_raw_message(self):
        creds = FakeCredentials()
        http = FakeHttpSession()
        with mailer.GmailApiSender(creds, http_session=http) as sender:
            sender.send(_message())

        self.assertEqual(len(http.posts), 1)
        call = http.posts[0]
        self.assertEqual(call["url"], mailer.GmailApiSender.SEND_URL)
        self.assertEqual(call["headers"]["Authorization"], "Bearer tok-abc")
        self.assertIn("raw", call["json"])

    def test_refreshes_an_expired_token_before_sending(self):
        creds = FakeCredentials(expired=True)
        http = FakeHttpSession()
        with mailer.GmailApiSender(creds, http_session=http) as sender:
            sender.send(_message())

        self.assertTrue(creds.refreshed)
        self.assertEqual(http.posts[0]["headers"]["Authorization"], "Bearer tok-refreshed")

    def test_does_not_refresh_a_token_that_is_still_valid(self):
        creds = FakeCredentials(expired=False)
        http = FakeHttpSession()
        with mailer.GmailApiSender(creds, http_session=http) as sender:
            sender.send(_message())
        self.assertFalse(creds.refreshed)


class SendBatchTransportTests(unittest.TestCase):
    """send_batch() itself, with a trivial in-memory sender — confirms the
    throttling/personalization loop no longer cares which transport it's
    given, now that SMTP connection logic moved out of it."""

    def test_works_with_any_object_offering_send_and_context_manager(self):
        sent = []

        class RecordingSender:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def send(self, msg): sent.append(msg["To"])

        cfg = mailer.MailerConfig(host="h", port=1, username="u", password="p",
                                   from_name="N", from_email="n@h.com", reply_to="n@h.com",
                                   send_delay_seconds=0)
        result = mailer.send_batch(
            cfg, RecordingSender(),
            recipients=["a@x.com", "b@x.com"],
            subject="s", html_template="<p>hi</p>", text_template="hi",
            unsubscribe_url_template="https://x/unsub?email={{email}}",
            tokens={"a@x.com": "t1", "b@x.com": "t2"},
        )
        self.assertEqual(result["sent"], ["a@x.com", "b@x.com"])
        self.assertEqual(sent, ["a@x.com", "b@x.com"])


if __name__ == "__main__":
    unittest.main()
