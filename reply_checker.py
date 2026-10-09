"""
reply_checker.py
Optional, best-effort reply detection over IMAP.

How it works:
- Every sent message got a unique Message-ID (see mailer.py), and the
  recipient row recorded sender_email — the FROM address it was actually
  sent through (see db.create_recipient). A reply lands in THAT mailbox,
  not necessarily any one fixed inbox: each user can send from their own
  connected Gmail (OAuth or a manually-entered App Password), not just
  the shared SMTP_* account in .env.
- So checking replies means, for each sender_email actually used:
  1. If it's a manually-connected Gmail (db.find_username_by_connected_email
     + db.get_manual_email_account), log into IMAP with that SAME App
     Password — Gmail accepts it for IMAP too, no separate setup needed.
  2. If it's an OAuth-connected Gmail, there's no password to use this
     way — IMAP would need XOAUTH2 (or the Gmail API's message list
     instead of IMAP entirely). Not built yet; skipped rather than
     silently checking the wrong mailbox.
  3. Otherwise (the shared SMTP_* identity, or an older row sent before
     sender_email was tracked) falls back to the static IMAP_HOST/
     IMAP_USERNAME/IMAP_PASSWORD in .env.
- When logged into the right mailbox, this scans recent messages and
  checks whether their In-Reply-To/References headers contain a
  Message-ID we're waiting on. If so, and the reply's From address
  matches the original recipient, it's recorded as a reply.
- OUTREACH_IMAP_HOST, if still set in .env, is checked too as an extra
  mailbox against every pending recipient — a legacy option from before
  per-user sending existed.

Limitations (worth knowing, not hidden):
- Some webmail/mobile clients don't set In-Reply-To correctly, so a
  genuine reply can occasionally be missed. This is a best-effort signal,
  not a guarantee — same caveat every real email tool has.
- This does NOT run automatically in the background. Call it from the
  "Check for replies" button on the dashboard, or wire it into a cron job
  / scheduled task that hits the /check-replies route periodically.
- Only scans the last SEARCH_WINDOW_DAYS days of mail by default, to keep
  each check fast.
"""

import email
import imaplib
import os
from email.header import decode_header
from email.utils import parseaddr

import db

SEARCH_WINDOW_DAYS = 30


def _imap_config(prefix: str = ""):
    """None if this mailbox isn't configured at all (prefix is optional).

    The host decides whether a PREFIXED mailbox counts as "configured" —
    it deliberately does NOT fall back to the unprefixed IMAP_HOST, or
    _imap_config(prefix="OUTREACH_") would be indistinguishable from "set"
    any time the shared IMAP_HOST exists, double-scanning the same inbox.
    Port/username/password can still fall back (a distinct host sharing
    the main account's port/credentials is a reasonable setup).
    """
    def _get(name, default=None):
        if prefix:
            value = os.environ.get(prefix + name)
            if value is not None:
                return value
        return os.environ.get(name, default)

    host = os.environ.get(prefix + "IMAP_HOST") if prefix else os.environ.get("IMAP_HOST")
    if not host:
        return None
    return {
        "host": host,
        "port": int(_get("IMAP_PORT", 993)),
        "username": _get("IMAP_USERNAME", _get("SMTP_USERNAME")),
        "password": _get("IMAP_PASSWORD", _get("SMTP_PASSWORD")),
    }


def _decode(value) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="ignore"))
        else:
            out.append(text)
    return "".join(out)


def _imap_config_for_sender(sender_email: str | None) -> dict | None:
    """Which mailbox to check for a message sent FROM sender_email — see
    module docstring for the priority order. None means "skip this one,
    there's no IMAP credential available for it yet" (an OAuth-only
    account), not "something went wrong"."""
    if sender_email:
        username = db.find_username_by_connected_email(sender_email)
        if username:
            manual = db.get_manual_email_account(username)
            if manual and manual["email"].lower() == sender_email.lower():
                return {
                    "host": "imap.gmail.com", "port": 993,
                    "username": manual["email"], "password": manual["app_password"],
                }
            return None  # OAuth-connected, not manual — no IMAP credential for it (yet)
    return _imap_config()  # shared SMTP_* identity, or an older row with no sender_email recorded


def check_for_replies() -> int:
    """Returns the number of newly-detected replies, across every mailbox
    that might have one for a pending recipient."""
    pending = db.get_all_recipients_for_reply_check()
    if not pending:
        return 0

    # Group by the RESOLVED mailbox (not sender_email directly) so two
    # different senders that both fall back to the same static config
    # don't open that mailbox twice.
    buckets: dict[tuple, tuple[dict, list[dict]]] = {}
    for recipient in pending:
        cfg = _imap_config_for_sender(recipient.get("sender_email"))
        if cfg is None:
            continue
        key = (cfg["host"], cfg["port"], cfg["username"])
        buckets.setdefault(key, (cfg, []))[1].append(recipient)

    found = sum(_scan_mailbox(cfg, group) for cfg, group in buckets.values())

    outreach_cfg = _imap_config(prefix="OUTREACH_")  # legacy extra mailbox, if still configured
    if outreach_cfg:
        found += _scan_mailbox(outreach_cfg, pending)

    return found


def _scan_mailbox(cfg: dict | None, pending: list[dict]) -> int:
    if cfg is None or not pending:
        return 0

    # message_id -> recipient row, for O(1) lookup while scanning the inbox
    by_message_id = {r["message_id"]: r for r in pending if r["message_id"]}
    if not by_message_id:
        return 0

    found = 0
    conn = imaplib.IMAP4_SSL(cfg["host"], cfg["port"])
    try:
        conn.login(cfg["username"], cfg["password"])
        conn.select("INBOX")

        status, data = conn.search(None, f'(SINCE "{_since_date()}")')
        if status != "OK":
            return 0

        for num in data[0].split():
            status, msg_data = conn.fetch(num, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue

            msg = email.message_from_bytes(msg_data[0][1])
            in_reply_to = msg.get("In-Reply-To", "")
            references = msg.get("References", "")
            haystack = f"{in_reply_to} {references}"

            for message_id, recipient in by_message_id.items():
                if message_id in haystack:
                    _, sender_email = parseaddr(_decode(msg.get("From", "")))
                    if sender_email.lower() == recipient["email"].lower():
                        db.record_reply(recipient["id"])
                        found += 1
    finally:
        conn.logout()

    return found


def _since_date() -> str:
    from datetime import datetime, timedelta
    d = datetime.utcnow() - timedelta(days=SEARCH_WINDOW_DAYS)
    return d.strftime("%d-%b-%Y")
