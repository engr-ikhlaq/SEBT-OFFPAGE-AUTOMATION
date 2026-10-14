"""Decide whether a page is about the keyword and accepts guest posts."""
from __future__ import annotations

import re

from .models import PageData
from .scoring import Scorer

STOP_TERMS = frozenset({"the", "and", "for", "with", "from", "that", "this", "your"})

# Phrases that mark a page as a generic guest-posting/link-building SERVICE
# or portfolio ("we'll get you published anywhere") rather than a real
# publication about the keyword's topic. These pages often list dozens of
# unrelated niches (client case studies, "categories we cover", etc.), so
# a single incidental keyword mention buried in one of those is not a
# genuine topical match — e.g. a "guest posting expert" portfolio with a
# client testimonial that happens to mention "peptide" once, among many
# completely unrelated ones for other industries.
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
# reduce a keyword TERM to a short root before matching it against a
# page's headline (see _has_term below, which already does prefix
# matching) - e.g. "registration" -> "regist", which is also a prefix of
# "register"/"registering"/"registered"/"registry" as they'd actually
# appear on a real page. Only the keyword side is stemmed; page text is
# never touched, so this works in both directions (a keyword that's the
# short form still matches a page using the long form, and vice versa)
# without needing a consistent canonical stem on both sides - the
# existing prefix match does that work once the keyword side is short
# enough. Deliberately not a real Porter stemmer (no new dependency to
# track for a personal tool); the >= 4 character floor keeps a stem from
# getting so short it starts matching unrelated words.
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


class RelevanceGate:
    """Rejects pages that are not about the keyword or do not accept guest posts.

    The topic must be clear. Either every keyword term appears in the title,
    heading or description, or the exact keyword phrase appears in the body.
    Scattered generic words in the body (for example 'ads' and 'pinterest'
    on a software page) are not enough. The page must also contain
    guest-post or contributor wording.
    """

    def __init__(self, scorer: Scorer) -> None:
        self._scorer = scorer

    def rejection_reason(self, keyword: str, page: PageData) -> str | None:
        terms = keyword_terms(keyword)
        if not terms:
            return "keyword has no usable terms"

        headline = " ".join((page.title, page.h1, page.meta)).lower()
        body = page.body.lower()
        phrase = " ".join(keyword.lower().split())

        # Stemmed, so "registration" (the keyword) matches a headline that
        # only says "register"/"registering"/"registered" - see _stem()'s
        # docstring for why only this side needs it. The exact-phrase body
        # check below is deliberately left unstemmed/literal; it's already
        # the looser of the two paths (any order, anywhere in the body),
        # so it doesn't need the same inflection tolerance.
        in_headline = all(_has_term(_stem(t), headline) for t in terms)
        phrase_in_body = _has_term(phrase, body)
        if not (in_headline or phrase_in_body):
            return "topic not clear: keyword not in title, heading or description"

        if self._scorer.intent_points(page.body) == 0:
            return "no guest-post or contributor wording"

        if any(signal in body for signal in MARKETPLACE_SIGNALS):
            return "looks like a generic guest-posting/link-building service, not a topical publication"

        return None
