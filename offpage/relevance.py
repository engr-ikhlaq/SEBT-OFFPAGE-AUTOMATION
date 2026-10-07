"""Decide whether a page is about the keyword and accepts guest posts."""
from __future__ import annotations

import re

from .models import PageData
from .scoring import Scorer

STOP_TERMS = frozenset({"the", "and", "for", "with", "from", "that", "this", "your"})


def keyword_terms(keyword: str) -> tuple[str, ...]:
    """Significant words of a keyword, e.g. 'snapchat cloaking agency' -> 3 terms."""
    return tuple(
        term
        for term in re.findall(r"[a-z0-9]+", keyword.lower())
        if len(term) >= 3 and term not in STOP_TERMS
    )


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

        in_headline = all(_has_term(t, headline) for t in terms)
        phrase_in_body = _has_term(phrase, body)
        if not (in_headline or phrase_in_body):
            return "topic not clear: keyword not in title, heading or description"

        if self._scorer.intent_points(page.body) == 0:
            return "no guest-post or contributor wording"
        return None
