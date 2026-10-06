"""Decide which search results are worth visiting."""
from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlparse

BLOCKED_HOSTS = frozenset({
    "facebook.com", "instagram.com", "twitter.com", "x.com",
    "linkedin.com", "youtube.com", "pinterest.com",
    "reddit.com", "tiktok.com", "quora.com",
    "medium.com", "github.com", "stackoverflow.com",
    "play.google.com", "apps.apple.com", "upwork.com", "fiverr.com",
    "freelancer.com",
})

BLOCKED_BUILDER_HOSTS = frozenset({
    "wix.com", "squarespace.com", "wordpress.com",
    "blogspot.com", "weebly.com", "shopify.com", "webflow.io",
})

BLOCKED_EXTENSIONS = (
    ".pdf", ".doc", ".docx", ".ppt", ".pptx",
    ".xls", ".xlsx", ".zip", ".rar", ".csv",
)

BLOCKED_PATH_TERMS = (
    "login", "signup", "register", "account",
    "dashboard", "admin", "wp-admin",
)


def domain_of(url: str) -> str:
    """Return the hostname without a leading 'www.'."""
    host = urlparse(url).hostname or ""
    return host.removeprefix("www.")


def _host_matches(host: str, domains: Iterable[str]) -> bool:
    """True when host equals a domain or is a subdomain of it."""
    return any(host == d or host.endswith("." + d) for d in domains)


class UrlFilter:
    """Hostname-based filter. Matches whole domains, never substrings."""

    def __init__(
        self,
        allowed_suffixes: Iterable[str] = (),
        blocked_hosts: Iterable[str] = BLOCKED_HOSTS | BLOCKED_BUILDER_HOSTS,
    ) -> None:
        # e.g. ".edu" or ".ac.uk". Empty means every suffix is allowed.
        self._allowed = tuple(s.lower() for s in allowed_suffixes)
        self._blocked = frozenset(blocked_hosts)

    def allows(self, url: str) -> bool:
        parts = urlparse(url)
        host = (parts.hostname or "").removeprefix("www.")

        if parts.scheme not in ("http", "https") or not host:
            return False
        if _host_matches(host, self._blocked):
            return False
        if self._allowed and not host.endswith(self._allowed):
            return False

        path = parts.path.lower()
        if path.endswith(BLOCKED_EXTENSIONS):
            return False
        return not any(term in path for term in BLOCKED_PATH_TERMS)
