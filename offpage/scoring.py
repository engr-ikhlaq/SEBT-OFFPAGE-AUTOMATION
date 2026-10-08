"""Score how promising a site is as an outreach target (0-100)."""
from __future__ import annotations

from collections.abc import Mapping

from .models import PageData

INTENT_PATTERNS: Mapping[str, tuple[str, ...]] = {
    "guest_post_core": (
        "write for us", "guest post", "guest posting", "guest article",
        "submit article", "submit post", "submit guest post",
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
}

INTENT_WEIGHTS: Mapping[str, int] = {
    "guest_post_core": 6,
    "contributor": 5,
    "editorial": 4,
    "pitch": 3,
    "monetization": 2,
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


class Scorer:
    def __init__(self, intent_patterns: Mapping[str, tuple[str, ...]] = INTENT_PATTERNS) -> None:
        # Flatten to (phrase, weight) once instead of looking up groups per call.
        self._intent = tuple(
            (phrase, INTENT_WEIGHTS[group])
            for group, phrases in intent_patterns.items()
            for phrase in phrases
        )

    def intent_points(self, body: str) -> int:
        text = body.lower()
        return min(sum(w for phrase, w in self._intent if phrase in text), INTENT_CAP)

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
        total = min(total + domain_points(domain), TOTAL_AFTER_DOMAIN_CAP)

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
