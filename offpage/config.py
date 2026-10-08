"""Runtime settings, loaded from environment variables (or a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_KEYWORDS: tuple[str, ...] = (
    "pept",
    "snapchat cloaking agency",
    "pinterest cloaking agency",
    "ads cloaking pinterest",
    "pinterest cloaking ads",
    "youtube cloaking agency",
    "ads cloaking snapchat",
)

# "[KEYWORD]" is replaced with each keyword at run time.
DEFAULT_QUERIES: tuple[str, ...] = (
    '[KEYWORD] "write for us"',
    '[KEYWORD] "guest post"',
    '[KEYWORD] "guest posting"',
    '[KEYWORD] "guest article"',
    '[KEYWORD] "submit article"',
    '[KEYWORD] "submit a guest post"',
    '[KEYWORD] "become a contributor"',
    '[KEYWORD] "contribute to our site"',
    '[KEYWORD] "submit post"',
    '[KEYWORD] "write for me"',
    '[KEYWORD] "contributor guidelines"',
    '[KEYWORD] "editorial guidelines"',
    '[KEYWORD] "submission guidelines"',
    '[KEYWORD] "writing guidelines"',
    '[KEYWORD] "guest post guidelines"',
    '[KEYWORD] "pitch us"',
    '[KEYWORD] "send your pitch"',
    '[KEYWORD] "submit your pitch"',
    '[KEYWORD] "article submission"',
    '[KEYWORD] "send article idea"',
    '[KEYWORD] "suggest a post"',
    'inurl:write-for-us [KEYWORD]',
    'inurl:guest-post [KEYWORD]',
    'inurl:contribute [KEYWORD]',
    'inurl:submit-article [KEYWORD]',
    'inurl:blog [KEYWORD] "write for us"',
    'intitle:"write for us" [KEYWORD]',
    'intitle:"guest post" [KEYWORD]',
    '[KEYWORD] "write for us" UK',
    '[KEYWORD] "guest post" UK',
    '[KEYWORD] "submit article" UK',
    '[KEYWORD] "contribute" UK',
    '[KEYWORD] "sponsored post"',
    '[KEYWORD] "advertise with us"',
    '[KEYWORD] "partnership opportunities"',
    '[KEYWORD] "collaborate with us"',
    '[KEYWORD] "brand collaboration"',
    '[KEYWORD] blog "write for us"',
    '[KEYWORD] blog "guest post"',
    '[KEYWORD] blog "contribute"',
    '[KEYWORD] blog "submit article"',
)


class ConfigError(ValueError):
    pass


def _csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    spreadsheet_id: str
    credentials_path: Path = Path("Credentials.json")
    worksheet_name: str | None = None
    seen_db_path: Path = Path("seen_domains.db")
    max_pages: int = 5
    # 1 means every confirmed lead is written to the sheet as soon as it is found.
    # Raise it to write in batches instead, which makes fewer, larger API calls.
    batch_size: int = 1
    crawl_workers: int = 6
    allowed_suffixes: tuple[str, ...] = ()
    keywords: tuple[str, ...] = DEFAULT_KEYWORDS
    queries: tuple[str, ...] = DEFAULT_QUERIES

    def __post_init__(self) -> None:
        if not self.spreadsheet_id:
            raise ConfigError("spreadsheet_id is required")
        if self.max_pages < 1:
            raise ConfigError("max_pages must be at least 1")
        if self.batch_size < 1:
            raise ConfigError("batch_size must be at least 1")
        if self.crawl_workers < 1:
            raise ConfigError("crawl_workers must be at least 1")

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        spreadsheet_id = os.environ.get("SPREADSHEET_ID", "").strip()
        if not spreadsheet_id:
            raise ConfigError("SPREADSHEET_ID is not set. Add it to .env or the environment.")
        return cls(
            spreadsheet_id=spreadsheet_id,
            credentials_path=Path(os.environ.get("CREDENTIALS_PATH", "Credentials.json")),
            worksheet_name=os.environ.get("WORKSHEET_NAME") or None,
            seen_db_path=Path(os.environ.get("SEEN_DB_PATH", "seen_domains.db")),
            max_pages=int(os.environ.get("MAX_PAGES", "5")),
            batch_size=int(os.environ.get("BATCH_SIZE", "1")),
            crawl_workers=int(os.environ.get("CRAWL_WORKERS", "6")),
            allowed_suffixes=_csv(os.environ.get("ALLOWED_SUFFIXES", "")),
        )
