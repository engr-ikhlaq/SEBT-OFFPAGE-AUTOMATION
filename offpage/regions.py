"""Google search regions the scraper can target - which Google domain to
search and what language/country parameters to send with it. The app has
always searched the United Kingdom's Google (google.co.uk); this makes
that one choice among several instead of a hardcoded constant, selected
per scrape from the Compose page's "Result region" dropdown (see
scrape_job.py, app.py's /scrape/start, templates/index.html).

Keeps hl (interface language) and gl (results country) matched to the
domain, since a mismatch between them (e.g. google.com with gl=uk) gives
noticeably worse region targeting than Google's own default pairing for
that domain.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    code: str
    label: str
    home_url: str
    lang: str  # Chrome's --lang startup argument, kept matched to hl/gl above


_REGIONS = (
    Region("uk", "United Kingdom", "https://www.google.co.uk/?hl=en-GB&gl=uk&pws=0", "en-GB"),
    Region("us", "United States", "https://www.google.com/?hl=en-US&gl=us&pws=0", "en-US"),
    Region("ca", "Canada", "https://www.google.ca/?hl=en-CA&gl=ca&pws=0", "en-CA"),
    Region("au", "Australia", "https://www.google.com.au/?hl=en-AU&gl=au&pws=0", "en-AU"),
    Region("ie", "Ireland", "https://www.google.ie/?hl=en-IE&gl=ie&pws=0", "en-IE"),
    Region("in", "India", "https://www.google.co.in/?hl=en-IN&gl=in&pws=0", "en-IN"),
    Region("nz", "New Zealand", "https://www.google.co.nz/?hl=en-NZ&gl=nz&pws=0", "en-NZ"),
    Region("za", "South Africa", "https://www.google.co.za/?hl=en-ZA&gl=za&pws=0", "en-ZA"),
)

REGIONS: dict[str, Region] = {r.code: r for r in _REGIONS}
DEFAULT_REGION = "uk"


def get_region(code: str | None) -> Region:
    """The matching Region, or the United Kingdom default if `code` is
    missing/unrecognized - never raises, so a tampered or stale form value
    degrades to the app's long-standing default instead of failing a scrape."""
    return REGIONS.get((code or "").strip().lower(), REGIONS[DEFAULT_REGION])
