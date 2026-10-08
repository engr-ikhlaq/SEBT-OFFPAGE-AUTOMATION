"""Plain data objects passed between pipeline stages."""
from __future__ import annotations

from dataclasses import dataclass

LEAD_HEADERS = (
    "Date",
    "Keyword",
    "Query",
    "Domain",
    "URL",
    "Email",
    "Contact Page",
    "Snippet",
    "Score",
)

MAX_CELL_CHARS = 500


@dataclass(frozen=True, slots=True)
class SearchResult:
    url: str
    domain: str
    snippet: str


@dataclass(frozen=True, slots=True)
class PageData:
    title: str = ""
    h1: str = ""
    meta: str = ""
    body: str = ""
    emails: frozenset[str] = frozenset()
    has_form: bool = False


@dataclass(frozen=True, slots=True)
class ContactInfo:
    emails: frozenset[str] = frozenset()
    contact_url: str = ""


@dataclass(frozen=True, slots=True)
class Lead:
    run_date: str
    keyword: str
    query: str
    domain: str
    url: str
    emails: tuple[str, ...]
    contact_url: str
    snippet: str
    score: int

    def to_row(self) -> list[str | int]:
        return [
            self.run_date,
            _cell(self.keyword),
            _cell(self.query),
            self.domain,
            _cell(self.url),
            _cell(", ".join(self.emails)),
            _cell(self.contact_url),
            _cell(self.snippet),
            self.score,
        ]


def _cell(value: str) -> str:
    """Flatten whitespace and cap length so one scraped value fits one sheet cell."""
    return " ".join(str(value).split())[:MAX_CELL_CHARS]
