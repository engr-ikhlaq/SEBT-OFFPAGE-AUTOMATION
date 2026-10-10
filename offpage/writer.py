"""Buffer leads into a sink, and mark domains as seen only after they are saved."""
from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Iterable
from typing import Protocol

from .models import Lead

log = logging.getLogger(__name__)


class SinkError(RuntimeError):
    """A batch could not be saved after all retries."""


class RowSink(Protocol):
    def append_rows(self, rows: list[list[str | int]]) -> None: ...


class SeenMarker(Protocol):
    def mark_seen(self, domains: Iterable[str]) -> None: ...


class LeadWriter:
    """Buffers leads and writes them once there are batch_size of them.

    With batch_size=1 (the application's default, set in Settings), a lead
    is handed to the sink the moment it is confirmed, one row at a time,
    rather than waiting for a bigger batch to accumulate.

    async_writes=True (used by the web app's scrape_job.py) sends each
    write to a background thread instead of blocking the caller on the
    Sheets API round trip - the scraper moves on to the next search/page
    while the previous lead's row is still being saved, so a slow Sheets
    call doesn't add its latency on top of the scrape's own. Call
    wait_all() before treating a run as finished: it blocks until every
    queued write has actually landed (or re-raises the first one that
    failed), so "done" never means "queued but maybe never saved."
    Without async_writes, add()/flush() behave exactly as before - this
    is what main.py's CLI path still gets, unchanged.

    A domain is recorded as seen only after its row is actually saved
    (synchronously or by the background thread). If a write fails, the
    leads stay pending/queued and their domains stay unseen, so the next
    run collects them again instead of losing them.
    """

    def __init__(self, sink: RowSink, store: SeenMarker, batch_size: int = 8,
                 async_writes: bool = False) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self._sink = sink
        self._store = store
        self._batch_size = batch_size
        self._pending: list[Lead] = []
        self._async = async_writes
        self._queue: queue.Queue[list[Lead] | None] | None = None
        self._worker: threading.Thread | None = None
        self._error: Exception | None = None
        if async_writes:
            self._queue = queue.Queue()
            self._worker = threading.Thread(target=self._drain, daemon=True)
            self._worker.start()

    @property
    def pending(self) -> int:
        """Leads not yet handed off to the sink - buffered locally below
        batch_size, not leads whose write is queued/in flight asynchronously
        (see wait_all() to block for those)."""
        return len(self._pending)

    def add(self, lead: Lead) -> None:
        self._pending.append(lead)
        if len(self._pending) >= self._batch_size:
            self.flush()

    def flush(self) -> int:
        if not self._pending:
            return 0
        leads = list(self._pending)
        if self._async:
            # Handed off to the queue - no longer this writer's to retry,
            # see wait_all()/the background thread for how a failure here
            # is surfaced instead.
            self._pending.clear()
            self._queue.put(leads)
        else:
            self._write(leads)  # raises on failure, leaving self._pending untouched for a retry
            self._pending.clear()
        return len(leads)

    def _write(self, leads: list[Lead]) -> None:
        # Raises on failure, leaving the store untouched for these leads -
        # synchronous mode leaves them in self._pending too (caller decides
        # whether to retry); async mode surfaces the error from wait_all().
        self._sink.append_rows([lead.to_row() for lead in leads])
        self._store.mark_seen(lead.domain for lead in leads)

    def _drain(self) -> None:
        while True:
            leads = self._queue.get()
            try:
                if leads is None:  # stop sentinel, see close()
                    return
                try:
                    self._write(leads)
                except Exception as exc:
                    log.exception("Background Sheet write failed for %d lead(s)", len(leads))
                    if self._error is None:
                        self._error = exc
            finally:
                self._queue.task_done()

    def wait_all(self) -> None:
        """Blocks until every write queued so far has actually been saved.
        Re-raises the first error any of them hit, if one did - call this
        before reporting a run as finished, not just after queuing the
        last lead. A no-op in synchronous mode, where every write already
        completed inline."""
        if not self._async:
            return
        self._queue.join()
        if self._error is not None:
            error, self._error = self._error, None
            raise error

    def close(self) -> None:
        """Stops the background write thread. Call after wait_all(), once
        nothing more will be added - leaves any already-queued-but-unwaited
        writes unprocessed, so prefer wait_all() first in normal use."""
        if self._async and self._worker is not None:
            self._queue.put(None)
            self._worker.join()
            self._worker = None
