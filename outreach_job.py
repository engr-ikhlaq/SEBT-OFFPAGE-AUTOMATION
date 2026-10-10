"""
outreach_job.py
Runs auto_outreach.send_pending_leads() in a background thread, so a web
request can start it and return immediately - the web side polls
get_status() the same way scrape_job.py does, rather than a page sitting
on a blocking POST with zero feedback until every email is sent.

Why this needs its own job runner: send_pending_leads() sends one email
at a time with a throttled, randomized delay between each (see
mailer._throttle_delay) - for any real number of leads that's easily a
couple of minutes, and a plain form POST gives the browser nothing to
show during that whole time but a spinner.

Only one run (per process) at a time - start() refuses while one is
already running, same reasoning as scrape_job: there's one shared
`sent`/`failed` tally being written to, not something to run twice
concurrently.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time

import auto_outreach

log = logging.getLogger("outreach_job")


@dataclasses.dataclass
class OutreachStatus:
    # idle | running | finished | stopped | error
    state: str = "idle"
    kind: str = ""  # "outreach" or "followups" - which button started this run
    username: str = ""
    total: int = 0
    done: int = 0
    sent_count: int = 0
    failed_count: int = 0
    current_email: str = ""
    error: str = ""
    started_at: float = 0.0
    elapsed_seconds: float = 0.0


_lock = threading.Lock()
_status = OutreachStatus()
_stop_requested = False


def get_status() -> dict:
    with _lock:
        status = dataclasses.asdict(_status)
    if status["state"] == "running" and status["started_at"]:
        status["elapsed_seconds"] = round(time.time() - status["started_at"], 1)
    del status["started_at"]  # a Unix timestamp isn't useful to the client; elapsed_seconds is
    return status


def is_busy() -> bool:
    with _lock:
        return _status.state == "running"


def stop() -> None:
    global _stop_requested
    with _lock:
        _stop_requested = True


def start(base_url: str, cfg, sender, backend, username: str) -> bool:
    """Starts sending to every pending lead in the background. False if a
    run is already in progress (one at a time, see module docstring)."""
    global _stop_requested
    with _lock:
        if _status.state == "running":
            return False
        _stop_requested = False
        _status.state = "running"
        _status.kind = "outreach"
        _status.username = username
        _status.total = 0
        _status.done = 0
        _status.sent_count = 0
        _status.failed_count = 0
        _status.current_email = ""
        _status.error = ""
        _status.started_at = time.time()
    thread = threading.Thread(target=_run_outreach, args=(base_url, cfg, sender, backend, username), daemon=True)
    thread.start()
    return True


def start_followups(base_url: str, cfg, sender, username: str) -> bool:
    """Same idea, for the "Follow-ups" button — send_due_followups() has no
    per-lead progress callback (it's normally a short list), so this just
    reports running/finished rather than a live done/total count."""
    global _stop_requested
    with _lock:
        if _status.state == "running":
            return False
        _stop_requested = False
        _status.state = "running"
        _status.kind = "followups"
        _status.username = username
        _status.total = 0
        _status.done = 0
        _status.sent_count = 0
        _status.failed_count = 0
        _status.current_email = ""
        _status.error = ""
        _status.started_at = time.time()
    thread = threading.Thread(target=_run_followups, args=(base_url, cfg, sender, username), daemon=True)
    thread.start()
    return True


def _on_progress(done: int, total: int, email: str, result: str) -> None:
    with _lock:
        _status.total = total
        _status.done = done
        _status.current_email = email
        if result == "sent":
            _status.sent_count += 1
        elif result == "failed":
            _status.failed_count += 1


def _should_stop() -> bool:
    with _lock:
        return _stop_requested


def _run_outreach(base_url: str, cfg, sender, backend, username: str) -> None:
    try:
        auto_outreach.send_pending_leads(
            base_url, cfg, sender, backend, username, on_progress=_on_progress, should_stop=_should_stop,
        )
    except Exception as exc:
        log.exception("Outreach run failed")
        with _lock:
            _status.state = "error"
            _status.error = str(exc)
        return
    with _lock:
        _status.state = "stopped" if _stop_requested else "finished"


def _run_followups(base_url: str, cfg, sender, username: str) -> None:
    try:
        result = auto_outreach.send_due_followups(base_url, cfg, sender, username)
    except Exception as exc:
        log.exception("Follow-up run failed")
        with _lock:
            _status.state = "error"
            _status.error = str(exc)
        return
    with _lock:
        _status.sent_count = len(result["sent"])
        _status.failed_count = len(result["failed"])
        _status.total = _status.sent_count + _status.failed_count
        _status.done = _status.total
        _status.state = "finished"
