"""Batch leads into a sink, and mark domains as seen only after they are saved."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from .models import Lead


class SinkError(RuntimeError):
    """A batch could not be saved after all retries."""


class RowSink(Protocol):
    def append_rows(self, rows: list[list[str | int]]) -> None: ...


class SeenMarker(Protocol):
    def mark_seen(self, domains: Iterable[str]) -> None: ...


class LeadWriter:
    """Buffers leads and writes them once there are batch_size of them.

    With batch_size=1 (the application's default, set in Settings), a lead
    is written the moment it is confirmed, one row at a time. A higher value
    trades that immediacy for fewer, larger Sheets API calls.

    A domain is recorded as seen only after its row is saved. If a write
    fails, the leads stay pending and their domains stay unseen, so the next
    run collects them again instead of losing them.
    """

    def __init__(self, sink: RowSink, store: SeenMarker, batch_size: int = 8) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self._sink = sink
        self._store = store
        self._batch_size = batch_size
        self._pending: list[Lead] = []

    @property
    def pending(self) -> int:
        return len(self._pending)

    def add(self, lead: Lead) -> None:
        self._pending.append(lead)
        if len(self._pending) >= self._batch_size:
            self.flush()

    def flush(self) -> int:
        if not self._pending:
            return 0
        leads = list(self._pending)
        # Raises on failure, leaving self._pending and the store untouched.
        self._sink.append_rows([lead.to_row() for lead in leads])
        self._store.mark_seen(lead.domain for lead in leads)
        self._pending.clear()
        return len(leads)
