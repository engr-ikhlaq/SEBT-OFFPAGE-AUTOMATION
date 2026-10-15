"""Pulls a site's own stated submission requirements straight off its
page - specifically, a word-count ask ("minimum 800 words", "500-1000
words", "1000+ words") - so an outreach pitch can mention it specifically
instead of reading like a template, without needing a paid LLM call to
write that line. Honest by construction: it only ever quotes a number
that's actually ON the page, never invents one.
"""
from __future__ import annotations

import re

# Longest/most specific first, so "500-1000 words" doesn't get matched by
# the single-number pattern before the range pattern gets a chance to.
# "word(s)" (singular allowed) since "1000+ word articles" is as common
# a phrasing as "1000+ words".
_RANGE = re.compile(r"(\d{2,5})\s*(?:-|to|–)\s*(\d{2,5})\s*words?", re.IGNORECASE)
_MINIMUM = re.compile(r"(?:minimum|at least|min\.?)\s*(?:of\s*)?(\d{2,5})\+?\s*words?", re.IGNORECASE)
_PLUS = re.compile(r"(\d{2,5})\+\s*words?", re.IGNORECASE)


def extract_word_count_requirement(text: str) -> str | None:
    """'500-1000 words' / '800+ words' / None if the page doesn't say."""
    if not text:
        return None
    match = _RANGE.search(text)
    if match:
        return f"{match.group(1)}-{match.group(2)} words"
    match = _MINIMUM.search(text) or _PLUS.search(text)
    if match:
        return f"{match.group(1)}+ words"
    return None
