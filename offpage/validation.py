"""Checks that emails and domains are usable before they reach the sheet.

Three layers, cheapest first:
1. Syntax: the address and its domain are well-formed.
2. Rejects: asset names, placeholders, tracking hosts and no-reply mailboxes.
3. MX lookup: the domain has mail servers. This confirms the domain can
   receive mail. It does not confirm that a particular mailbox exists.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

import dns.exception
import dns.resolver

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_TLD = r"[a-z]{2,63}"
_DOMAIN = r"(?:" + _LABEL + r"\.)+" + _TLD
_EMAIL = r"[a-z0-9](?:[a-z0-9._%+-]{0,62}[a-z0-9])?@" + _DOMAIN

DOMAIN_RE = re.compile(_DOMAIN)
EMAIL_RE = re.compile(_EMAIL)

# File extensions that look like a TLD, e.g. "logo@2x.png".
ASSET_TLDS = frozenset({
    "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp",
    "js", "css", "woff", "woff2", "ttf", "eot", "mp4", "mp3",
})

PLACEHOLDER_DOMAINS = frozenset({
    "example.com", "example.org", "example.net", "domain.com",
    "yourdomain.com", "email.com", "test.com", "localhost",
    "sentry.io", "wixpress.com", "sentry-next.wixpress.com",
})

NO_REPLY_LOCALS = frozenset({
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "bounces",
})


def is_plausible_domain(host: str) -> bool:
    """True for hostnames like 'example.co.uk'. Rejects '1', '50+' and similar."""
    host = host.lower().removeprefix("www.")
    return len(host) <= 253 and DOMAIN_RE.fullmatch(host) is not None


def is_valid_email_syntax(email: str) -> bool:
    address = email.strip().lower()
    if len(address) > 254 or ".." in address or EMAIL_RE.fullmatch(address) is None:
        return False

    local, domain = address.rsplit("@", 1)
    if local in NO_REPLY_LOCALS or domain.rsplit(".", 1)[1] in ASSET_TLDS:
        return False
    return not any(domain == d or domain.endswith("." + d) for d in PLACEHOLDER_DOMAINS)


class EmailValidator:
    def __init__(self, resolver: dns.resolver.Resolver | None = None, timeout: float = 5.0) -> None:
        self._resolver = resolver or dns.resolver.Resolver()
        self._resolver.lifetime = timeout
        self._mx_cache: dict[str, bool] = {}

    def domain_accepts_mail(self, domain: str) -> bool:
        if domain not in self._mx_cache:
            self._mx_cache[domain] = self._lookup_mx(domain)
        return self._mx_cache[domain]

    def _lookup_mx(self, domain: str) -> bool:
        try:
            return len(self._resolver.resolve(domain, "MX")) > 0
        except dns.exception.DNSException:
            # NXDOMAIN, no MX record, or a timeout: not verifiable, so reject.
            return False

    def filter_valid(self, emails: Iterable[str]) -> list[str]:
        """Return the unique valid addresses, lower-cased and sorted."""
        valid: set[str] = set()
        for email in emails:
            if not is_valid_email_syntax(email):
                continue
            address = email.strip().lower()
            if self.domain_accepts_mail(address.rsplit("@", 1)[1]):
                valid.add(address)
        return sorted(valid)
