"""Persistent memory of domains already processed, so reruns skip them."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path


class SeenStore:
    # Same table name as the original scripts, so existing .db files still work.
    _SCHEMA = "CREATE TABLE IF NOT EXISTS seen (domain TEXT PRIMARY KEY)"

    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.execute(self._SCHEMA)
        self._conn.commit()

    def __enter__(self) -> SeenStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def is_seen(self, domain: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM seen WHERE domain = ?", (domain,)).fetchone()
        return row is not None

    def mark_seen(self, domains: Iterable[str]) -> None:
        self._conn.executemany(
            "INSERT OR IGNORE INTO seen (domain) VALUES (?)",
            [(d,) for d in domains],
        )
        self._conn.commit()
