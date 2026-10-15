"""Decide whether a page is a genuine guest-posting opportunity."""
from __future__ import annotations

from .models import PageData
from .scoring import Scorer

# Phrases that mark a page as a generic guest-posting/link-building SERVICE
# or portfolio ("we'll get you published anywhere") rather than a real
# publication that happens to take guest posts. These pages often list
# dozens of unrelated niches (client case studies, "categories we cover",
# etc.), so having guest-post wording at all is not enough on its own -
# e.g. a "guest posting expert" portfolio advertising placements on
# Forbes/TechCrunch/etc. for any industry, not a site that is itself
# about any particular topic.
MARKETPLACE_SIGNALS: tuple[str, ...] = (
    "guest posting service", "guest post service", "guest posting expert",
    "link building service", "link building agency", "link building package",
    "seo agency", "backlink service", "backlink package", "buy backlinks",
    "buy guest post", "buy guest posts", "guest post package",
    "domain authority", "da/dr", "da & dr", "niche edit", "niche edits",
    "press release distribution", "guaranteed placement", "pbn links",
    "top-tier publications", "publish on top-tier", "publish you on",
)


class RelevanceGate:
    """Rejects pages that are not genuine guest-posting opportunities.

    Topic match against the keyword is NOT a hard requirement here - a
    site that takes guest posts on a different subject than the keyword
    is still a real outreach lead, just a weaker one, so it is captured
    rather than discarded. See Scorer.score()'s relevance component for
    how topic match instead affects the lead's score, so a closer match
    (or a site with a proven track record of guest posts) ranks higher
    without unrelated-but-genuine opportunities being thrown away.

    Only two things reject a page outright: it has no actual guest-post/
    contributor wording at all (so it is not a "guest posting
    opportunity" to begin with), or it reads like a paid guest-posting/
    link-building service or portfolio rather than a real publication.
    """

    def __init__(self, scorer: Scorer) -> None:
        self._scorer = scorer

    def rejection_reason(self, page: PageData) -> str | None:
        if self._scorer.intent_points(page.body) == 0:
            return "no guest-post or contributor wording"

        body = page.body.lower()
        if any(signal in body for signal in MARKETPLACE_SIGNALS):
            return "looks like a generic guest-posting/link-building service, not a topical publication"

        return None
