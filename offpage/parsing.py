"""Turn HTML into PageData and pull out email addresses."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .models import PageData

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,}")

# Image and asset names such as "logo@2x.png" match the regex but are not addresses.
NOT_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".js", ".css")

NON_VISIBLE_TAGS = ("script", "style", "noscript")
MAILTO_PREFIX = "mailto:"


def extract_emails(text: str) -> frozenset[str]:
    found = {match.lower() for match in EMAIL_RE.findall(text or "")}
    return frozenset(e for e in found if not e.endswith(NOT_EMAIL_SUFFIXES))


class PageParser:
    def parse(self, html: str) -> PageData:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(NON_VISIBLE_TAGS):
            tag.decompose()

        title = soup.title.get_text(strip=True) if soup.title else ""
        h1 = soup.h1.get_text(" ", strip=True) if soup.h1 else ""
        meta_tag = soup.find("meta", attrs={"name": "description"})
        meta = (meta_tag.get("content") or "") if meta_tag else ""
        body = soup.get_text(" ", strip=True)

        mailto_targets = [
            a["href"][len(MAILTO_PREFIX):].split("?")[0]
            for a in soup.find_all("a", href=True)
            if a["href"].lower().startswith(MAILTO_PREFIX)
        ]
        emails = extract_emails(body) | extract_emails(" ".join(mailto_targets))

        return PageData(
            title=title,
            h1=h1,
            meta=meta,
            body=body,
            emails=emails,
            has_form=soup.find("form") is not None,
        )
