"""Score how promising a site is as an outreach target (0-100)."""
from __future__ import annotations

from collections.abc import Mapping

from .models import PageData

INTENT_PATTERNS: Mapping[str, tuple[str, ...]] = {
    "guest_post_core": (
        "write for us", "guest post", "guest posting", "guest article",
        "guest blogging", "guest blogger", "accepting guest posts",
        "accepting guest contributors", "submit article", "submit post",
        "submit guest post",
    ),
    "contributor": (
        "become a contributor", "contribute to our site",
        "contributor guidelines", "writing guidelines",
    ),
    "editorial": (
        "editorial guidelines", "submission guidelines", "guest post guidelines",
    ),
    "pitch": (
        "pitch us", "send your pitch", "submit your pitch",
        "send article idea", "suggest a post",
    ),
    "monetization": (
        "sponsored post", "advertise with us",
        "brand collaboration", "partnership opportunities",
    ),
    # A published byline, not a submissions page - proof the site has
    # actually run someone else's content before, not just that it
    # claims to be open to it.
    "proven_track_record": (
        "guest post by", "this is a guest post", "guest author",
        "guest contributor", "contributed by",
    ),
}

INTENT_WEIGHTS: Mapping[str, int] = {
    "guest_post_core": 6,
    "contributor": 5,
    "editorial": 4,
    "pitch": 3,
    "monetization": 2,
    "proven_track_record": 6,
}

DOMAIN_POINTS: tuple[tuple[str, int], ...] = (
    (".edu", 15),
    (".gov", 15),
    (".org", 10),
    (".co.uk", 8),
    (".com", 5),
)

# Section caps, applied in order. They mirror the original weighting.
RELEVANCE_CAP = 40
INTENT_CAP = 25
TOTAL_AFTER_INTENT_CAP = 65
TOTAL_AFTER_DOMAIN_CAP = 80
TOTAL_AFTER_CONTACT_CAP = 90
SCORE_CAP = 100


def domain_points(domain: str) -> int:
    for suffix, points in DOMAIN_POINTS:
        if domain.endswith(suffix):
            return points
    return 0


# When a suffix's real reply rate is known (see db.get_tld_reply_rates),
# it's scaled onto this range instead of the static DOMAIN_POINTS guess -
# set a little above the richest static entry (.edu/.gov = 15) so a
# suffix that actually gets replies in practice can clearly outrank one
# that merely LOOKS authoritative but never converts.
LEARNED_DOMAIN_POINTS_SCALE = 30


class Scorer:
    def __init__(
        self,
        intent_patterns: Mapping[str, tuple[str, ...]] = INTENT_PATTERNS,
        tld_reply_rates: Mapping[str, float] | None = None,
    ) -> None:
        # Flatten to (phrase, weight) once instead of looking up groups per call.
        self._intent = tuple(
            (phrase, INTENT_WEIGHTS[group])
            for group, phrases in intent_patterns.items()
            for phrase in phrases
        )
        # Real reply-rate per domain suffix, learned from this tool's own
        # past outreach history (see db.get_tld_reply_rates) - None/empty
        # means no real data yet (a fresh install, or too few sends so
        # far), so domain scoring falls back to the static table below,
        # exactly as it worked before this existed.
        self._tld_reply_rates = tld_reply_rates or {}

    def intent_points(self, body: str) -> int:
        text = body.lower()
        return min(sum(w for phrase, w in self._intent if phrase in text), INTENT_CAP)

    def _domain_points(self, domain: str) -> int:
        for suffix, static_points in DOMAIN_POINTS:
            if domain.endswith(suffix):
                rate = self._tld_reply_rates.get(suffix)
                return round(rate * LEARNED_DOMAIN_POINTS_SCALE) if rate is not None else static_points
        return 0

    def score(
        self,
        keyword: str,
        page: PageData,
        domain: str,
        has_email: bool,
        has_contact: bool,
    ) -> int:
        kw = keyword.lower()
        title = page.title.lower()
        h1 = page.h1.lower()
        meta = page.meta.lower()
        body = page.body.lower()

        # 1. Relevance: where the keyword appears on the page.
        relevance = 0
        if kw in title:
            relevance += 12
        if kw in h1:
            relevance += 10
        if kw in meta:
            relevance += 6
        relevance += 2 * sum(word in body for word in kw.split())
        relevance += 3 * min(body.count(kw), 3)
        total = min(relevance, RELEVANCE_CAP)

        # 2. Intent phrases in the body.
        total = min(total + self.intent_points(page.body), TOTAL_AFTER_INTENT_CAP)

        # 3. Domain quality.
        total = min(total + self._domain_points(domain), TOTAL_AFTER_DOMAIN_CAP)

        # 4. Contact quality.
        total += 6 if has_email else 0
        total += 4 if has_contact else 0
        total = min(total, TOTAL_AFTER_CONTACT_CAP)

        # 5. Basic SEO hygiene.
        total += 2 if len(page.title) > 10 else 0
        total += 2 if len(page.meta) > 20 else 0
        total += 2 if len(page.h1) > 5 else 0
        total += 2 if body.count("http") > 2 else 0
        total += 2 if "https" in body else 0

        return min(total, SCORE_CAP)
