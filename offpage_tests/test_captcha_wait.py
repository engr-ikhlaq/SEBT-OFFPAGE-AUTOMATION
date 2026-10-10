"""offpage.browser.GoogleSearch._wait_for_captcha - waits for a human to
solve a CAPTCHA (detected via current_url) instead of failing the scrape
immediately, with a timeout and should_stop() (the Stop button) as the
two ways out besides it actually being solved. No real Selenium/Chrome
involved - current_url is faked, so this runs in milliseconds."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from offpage.browser import GoogleSearch, SearchBlockedError


class FakeSession:
    """current_url pops through a fixed sequence of URLs, one per read,
    then repeats the last one - lets a test script "still a CAPTCHA,
    still a CAPTCHA, ... now solved" across successive polls."""
    def __init__(self, urls: list[str]):
        self._urls = list(urls)
        self._i = 0

    @property
    def current_url(self) -> str:
        url = self._urls[min(self._i, len(self._urls) - 1)]
        self._i += 1
        return url


def _search(session, **kwargs) -> GoogleSearch:
    gs = GoogleSearch(session, **kwargs)
    gs.CAPTCHA_POLL_SECONDS = 0.001  # don't actually wait seconds in tests
    return gs


class WaitForCaptchaTests(unittest.TestCase):
    def test_no_captcha_returns_immediately_without_notifying(self):
        session = FakeSession(["https://www.google.com/search?q=x"])
        notifications = []
        gs = _search(session, on_captcha=notifications.append)
        gs._wait_for_captcha()  # must not raise or hang
        self.assertEqual(notifications, [])

    def test_waits_then_resumes_once_the_captcha_clears(self):
        session = FakeSession([
            "https://www.google.com/sorry/index",
            "https://www.google.com/sorry/index",
            "https://www.google.com/sorry/index",
            "https://www.google.com/search?q=x",  # solved - forwarded back to the real results
        ])
        notifications = []
        gs = _search(session, on_captcha=notifications.append)
        gs._wait_for_captcha()  # must not raise
        self.assertEqual(notifications, [True, False])  # told waiting started, then ended

    def test_gives_up_after_the_timeout(self):
        session = FakeSession(["https://www.google.com/sorry/index"] * 5)
        gs = _search(session)
        gs.CAPTCHA_TIMEOUT_SECONDS = 0  # times out on the very first check
        with self.assertRaises(SearchBlockedError):
            gs._wait_for_captcha()

    def test_stop_button_interrupts_the_wait(self):
        session = FakeSession(["https://www.google.com/sorry/index"] * 5)
        gs = _search(session, should_stop=lambda: True)
        with self.assertRaises(SearchBlockedError):
            gs._wait_for_captcha()

    def test_on_captcha_false_still_fires_after_a_timeout_or_stop(self):
        session = FakeSession(["https://www.google.com/sorry/index"] * 5)
        notifications = []
        gs = _search(session, on_captcha=notifications.append)
        gs.CAPTCHA_TIMEOUT_SECONDS = 0
        with self.assertRaises(SearchBlockedError):
            gs._wait_for_captcha()
        self.assertEqual(notifications, [True, False])  # cleared even though it failed


if __name__ == "__main__":
    unittest.main()
