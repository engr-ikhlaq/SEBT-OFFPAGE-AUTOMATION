"""Plain data objects passed between pipeline stages."""
from __future__ import annotations

from dataclasses import dataclass

# One schema, used everywhere a lead gets written (the CLI's raw Sheet,
# a user's personal Sheet, and the local-file fallback - see
# local_store.py, which imports this rather than keeping its own
# separate copy). Status/Sent Time/Error are filled in later, once an
# outreach email actually goes out (see db.py/write_result); DA/Traffic
# Rank are filled in at scrape time by offpage.authority. Both groups
# are placed at the end deliberately: any sheet created before one of
# them existed gets its header row extended in place rather than
# reordered (see each sink's ensure_headers()), so adding a new column
# here is forward-compatible with every sheet already out there instead
# of risking misaligning real, already-written data.
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
    "Status",
    "Sent Time",
    "Error",
    "DA",
    "Traffic Rank",
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
    # Authority/traffic signals (see offpage.authority) - None when not
    # looked up (e.g. no Open PageRank API key configured) or not found,
    # which is shown as a blank cell rather than a misleading 0.
    da_score: float | None = None
    traffic_rank: int | None = None

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
            "",  # Status - set later, once an outreach email is actually sent
            "",  # Sent Time
            "",  # Error
            self.da_score if self.da_score is not None else "",
            self.traffic_rank if self.traffic_rank is not None else "",
        ]


def _cell(value: str) -> str:
    """Flatten whitespace and cap length so one scraped value fits one sheet cell."""
    return " ".join(str(value).split())[:MAX_CELL_CHARS]
