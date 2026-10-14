"""Domain authority/traffic signals for a candidate site, so a lead isn't
just "relevant" but also worth pursuing for its SEO value - a separate
check from RelevanceGate (topic/intent).

Two free, real, independently-verifiable data sources - nothing here is
invented or estimated locally:

- Open PageRank (openpagerank.com): a free API, 0-10 link-graph
  authority score - the closest free, real stand-in for a Moz-style
  "Domain Authority" number (not literally Moz's trademarked metric, but
  a genuine, independently computed authority signal derived from the
  web's actual link graph). Needs a free API key (OPEN_PAGERANK_API_KEY
  in .env, see .env.example for how to get one) - skipped entirely
  (returns None, never raises) if not configured, so this never blocks
  scraping from working without it.
- Tranco (tranco-list.eu): a free, no-signup-needed global top-10M
  domains-by-traffic list used in web/security research (cited by
  Mozilla, Cloudflare, academic papers) - a domain's traffic rank out of
  roughly 10 million sites (lower = more traffic). Always available, no
  API key, but rate-limited, so only looked up for candidates that have
  already passed every other check (relevance, email) rather than every
  raw search result.

Looked up once per domain per scrape run and cached in-process, since
SeenStore already guarantees the same domain is never re-checked within
one run.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

OPEN_PAGERANK_URL = "https://openpagerank.com/api/v1.0/getPageRank"
TRANCO_URL = "https://tranco-list.eu/api/ranks/domain/{domain}"


@dataclass(frozen=True, slots=True)
class AuthoritySignals:
    da_score: float | None  # Open PageRank, 0-10 - None if not configured or not found
    traffic_rank: int | None  # Tranco rank out of ~10,000,000 - None if not found/unavailable

    def meets(self, min_da: float, max_rank: int | None) -> bool:
        """True unless a configured minimum/maximum is set AND the signal
        actually on hand for this domain falls short of it. A signal that
        couldn't be looked up (None) never blocks a lead by itself - an
        unranked site isn't necessarily low quality, just unmeasured, and
        a hard rejection on missing data would silently throw out leads
        whenever Open PageRank isn't configured or Tranco is rate-limited."""
        if min_da > 0 and self.da_score is not None and self.da_score < min_da:
            return False
        if max_rank is not None and self.traffic_rank is not None and self.traffic_rank > max_rank:
            return False
        return True


class AuthorityChecker:
    def __init__(self, open_pagerank_key: str | None = None,
                 http: requests.Session | None = None) -> None:
        self._key = (open_pagerank_key or "").strip() or None
        self._http = http or requests.Session()
        self._cache: dict[str, AuthoritySignals] = {}
        self._lock = threading.Lock()  # candidates are checked one at a time today, but cheap insurance

    def lookup(self, domain: str) -> AuthoritySignals:
        with self._lock:
            cached = self._cache.get(domain)
        if cached is not None:
            return cached
        signals = AuthoritySignals(da_score=self._open_pagerank(domain), traffic_rank=self._tranco(domain))
        with self._lock:
            self._cache[domain] = signals
        return signals

    def _open_pagerank(self, domain: str) -> float | None:
        if not self._key:
            return None
        try:
            resp = self._http.get(
                OPEN_PAGERANK_URL, params={"domains[]": domain},
                headers={"API-OPR": self._key}, timeout=8,
            )
            resp.raise_for_status()
            results = resp.json().get("response", [])
            if results and results[0].get("status_code") == 200:
                return float(results[0]["page_rank_decimal"])
        except Exception:
            log.warning("Open PageRank lookup failed for %s", domain, exc_info=True)
        return None

    def _tranco(self, domain: str, retries: int = 2) -> int | None:
        for attempt in range(retries + 1):
            try:
                resp = self._http.get(TRANCO_URL.format(domain=domain), timeout=8)
                if resp.status_code == 429:
                    if attempt < retries:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    return None
                resp.raise_for_status()
                ranks = resp.json().get("ranks", [])
                # Entries are newest-first (verified against the live API);
                # no entry at all means the domain isn't in Tranco's top
                # ~10M, i.e. traffic too low to rank, not an error.
                return int(ranks[0]["rank"]) if ranks else None
            except Exception:
                log.warning("Tranco lookup failed for %s", domain, exc_info=True)
                return None
        return None
