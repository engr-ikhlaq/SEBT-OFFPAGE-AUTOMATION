"""General-purpose "is this the same subject area" check.

Earlier versions of this gate tried a fixed list of categories
(technology, health, food, ...) to catch a page from a plainly
different subject than the keyword. That doesn't generalize - there
are effectively unlimited real-world niches and nobody can maintain a
list that covers all of them. This instead measures semantic similarity
between the keyword and the page's own text using a local word-vector
model (spaCy's en_core_web_md): words that tend to appear in similar
contexts across a huge amount of real text end up with similar vectors,
so "sim card" and "mobile network" land close together, while "sim
card" and "chocolate cake" land far apart - without anyone having had
to say so by hand. Runs entirely offline/locally once the model is
downloaded, so there's no per-request cost and nothing to keep paying
for or renewing.
"""
from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger(__name__)

MODEL_NAME = "en_core_web_md"

_nlp = None
_load_failed = False


def _model():
    """Loaded once per process, lazily (most callers never need it - e.g.
    a page that's rejected for having no guest-post wording at all never
    reaches a similarity check, and a page that already uses the
    keyword directly never needs this at all). Only the tokenizer and
    word vectors are kept; the parser/NER/tagger/etc. are disabled since
    nothing here uses them and they cost real memory and CPU to run for
    no benefit."""
    global _nlp, _load_failed
    if _nlp is not None or _load_failed:
        return _nlp
    try:
        import spacy
        _nlp = spacy.load(
            MODEL_NAME,
            disable=["parser", "ner", "tagger", "lemmatizer", "attribute_ruler"],
        )
    except Exception:
        log.warning(
            "Semantic topic matching unavailable (%s not installed) - "
            "the subject-match check is skipped rather than guessed at", MODEL_NAME,
        )
        _load_failed = True
        return None
    return _nlp


# Comparing the whole body of a long page is unnecessary (and slower)
# for what is just a "same general subject or not" check.
MAX_CHARS_COMPARED = 2000

# A plain sentence-level average (spaCy's own Doc.similarity) pools in
# every stop word and filler phrase too - and every "write for us" page
# shares the same guest-post boilerplate regardless of its actual
# subject, which dilutes the real signal enough that a phones keyword
# ends up looking about as similar to a food blog as to a genuine tech
# blog. Averaging only the meaningful content words instead (no stop
# words, no short/non-alphabetic tokens) gave a much cleaner split in
# testing - see offpage_tests/test_semantics.py for the calibration data.
_MIN_TOKEN_LEN = 3


def _content_vector(text: str) -> np.ndarray | None:
    nlp = _model()
    if nlp is None:
        return None
    tokens = [
        t for t in nlp(text[:MAX_CHARS_COMPARED])
        if t.has_vector and t.is_alpha and not t.is_stop and len(t.text) >= _MIN_TOKEN_LEN
    ]
    if not tokens:
        return None
    return np.mean([t.vector for t in tokens], axis=0)


def similarity(a: str, b: str) -> float | None:
    """Cosine similarity, roughly 0-1 for real English text, between the
    averaged content-word vectors of two texts. None when the model
    isn't installed, or when one side has no content word the model
    recognizes a vector for (e.g. a brand-new coined term) - callers
    should treat None as "can't tell" and not reject on it, the same as
    an unrecognized keyword before this existed."""
    vec_a, vec_b = _content_vector(a), _content_vector(b)
    if vec_a is None or vec_b is None:
        return None
    norm_a, norm_b = np.linalg.norm(vec_a), np.linalg.norm(vec_b)
    if not norm_a or not norm_b:
        return None
    return float(np.dot(vec_a, vec_b) / (norm_a * norm_b))
