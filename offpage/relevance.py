"""Decide whether a page is a genuine, same-subject guest-posting opportunity."""
from __future__ import annotations

import re

from . import semantics
from .models import PageData
from .scoring import Scorer

STOP_TERMS = frozenset({"the", "and", "for", "with", "from", "that", "this", "your"})

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


def keyword_terms(keyword: str) -> tuple[str, ...]:
    """Significant words of a keyword, e.g. 'snapchat cloaking agency' -> 3 terms."""
    return tuple(
        term
        for term in re.findall(r"[a-z0-9]+", keyword.lower())
        if len(term) >= 3 and term not in STOP_TERMS
    )


# A short, dependency-free list of common English suffixes, longest first
# so "operations" strips the whole "ations" rather than just "s". Used to
# reduce a keyword TERM to a short root before matching it against the
# page - e.g. "registration" -> "regist", which is also a prefix of
# "register"/"registering"/"registered"/"registry" as they'd actually
# appear on a real page. Only the keyword side is stemmed; page text is
# never touched, so this works in both directions without needing a
# consistent canonical stem on both sides - the prefix match in
# _has_term does that work once the keyword side is short enough.
_STEM_SUFFIXES: tuple[str, ...] = (
    "ations",
    "ation", "ators", "ingly",
    "ator", "edly", "tion", "sion",
    "ing", "ers", "ied", "ies",
    "er", "ed", "es",
    "s",
)


def _stem(word: str) -> str:
    for suffix in _STEM_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def _has_term(term: str, text: str) -> bool:
    """Match at the start of a word, so 'pept' matches 'peptide'."""
    return re.search(r"\b" + re.escape(term), text) is not None


# Below this, a page's content is not credibly the same subject as the
# keyword at all - calibrated against real phrase pairs (see
# offpage_tests/test_semantics.py): clearly wrong-subject pairs (a phones
# keyword against a food or heart-surgery page, say) land around
# 0.18-0.28; genuinely related pairs, even with no literal keyword
# overlap (a "sim registration" keyword against a telecom blog that never
# uses that exact phrase), land at 0.33 and above. The gap between those
# two groups is narrow and imperfect - this is an approximate signal, not
# a precise one - so the floor sits just above the highest wrong-subject
# score seen in testing, accepting that a handful of genuinely adjacent
# (but not literally keyword-matching) leads may be missed in exchange
# for reliably catching the clearly-wrong ones. This is only ever
# consulted as a last resort - see _subject_mismatch_reason below.
TOPIC_SIMILARITY_FLOOR = 0.30


class RelevanceGate:
    """Rejects pages that are not genuine, same-subject guest-posting
    opportunities.

    The exact keyword phrase is NOT required - a site that covers the
    same subject without those literal words (a telecom blog for a "sim
    registration" keyword, say) is still a real, relevant lead. What IS
    still required: when neither the keyword nor any of its significant
    words appears anywhere on the page at all, the page's own content
    must be semantically close enough to the keyword (see
    offpage/semantics.py - a local word-vector model, not a maintained
    list of categories, so this generalizes to any keyword/niche without
    anyone having to say in advance what counts as "the same" category
    for it). A phones keyword should not pull in a food blog or a
    heart-surgery site just because that site happens to take guest
    posts. A keyword with no recognizable content words (an unusual
    coined term, say) is left alone rather than guessed at.

    Beyond that, a page must have actual guest-post/contributor wording
    (it's not an "opportunity" otherwise), and must not read like a paid
    link-building service/portfolio rather than a real publication.
    How CLOSELY a page matches the keyword - beyond just "same subject
    or not" - is not itself a pass/fail gate; it feeds Scorer.score()'s
    relevance component instead, so a closer match ranks higher without
    a looser-but-same-subject match being thrown away.
    """

    def __init__(self, scorer: Scorer) -> None:
        self._scorer = scorer

    def rejection_reason(self, keyword: str, page: PageData) -> str | None:
        if self._scorer.intent_points(page.body) == 0:
            return "no guest-post or contributor wording"

        full_text = " ".join((page.title, page.h1, page.meta, page.body)).lower()

        if any(signal in full_text for signal in MARKETPLACE_SIGNALS):
            return "looks like a generic guest-posting/link-building service, not a topical publication"

        return self._subject_mismatch_reason(keyword, full_text)

    def _subject_mismatch_reason(self, keyword: str, full_text: str) -> str | None:
        terms = keyword_terms(keyword)
        if terms and any(_has_term(_stem(t), full_text) for t in terms):
            return None  # the page actually uses the keyword (or a close form of it) - trust that directly

        # The full page text (title through body) is used, not just the
        # title/heading - a short title alone averages too few content
        # words to be a reliable vector (tested; it's noisier, not
        # cleaner, than the fuller text despite also carrying some nav/ad
        # noise - see offpage_tests/test_semantics.py for the numbers).
        sim = semantics.similarity(keyword, full_text)
        if sim is None or sim >= TOPIC_SIMILARITY_FLOOR:
            return None  # same/related subject, or the model couldn't confidently tell either way

        return f'different subject area: no sign of "{keyword}" or anything close to it on the page'
