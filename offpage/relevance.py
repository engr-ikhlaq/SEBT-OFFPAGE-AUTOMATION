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


class RelevanceGate:
    """Rejects pages that do not match the keyword's topic or the guest-post intent.

    Every keyword term must appear in the page (matched at the start of a word,
    so 'pept' matches 'peptide'). The page must also contain guest-post or
    contributor wording. Both checks apply to the page text, not the URL.
    """

    def __init__(self, scorer: Scorer) -> None:
        self._scorer = scorer

    def rejection_reason(self, keyword: str, page: PageData) -> str | None:
        terms = keyword_terms(keyword)
        if not terms:
            return "keyword has no usable terms"

        text = " ".join((page.title, page.h1, page.meta, page.body)).lower()
        missing = [t for t in terms if re.search(r"\b" + re.escape(t), text) is None]
        if missing:
            return "topic terms not found: " + ", ".join(missing)

        if self._scorer.intent_points(page.body) == 0:
            return "no guest-post or contributor wording"
        return None
