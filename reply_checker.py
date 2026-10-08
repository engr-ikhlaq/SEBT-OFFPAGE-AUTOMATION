"""
reply_checker.py
Optional, best-effort reply detection over IMAP.

How it works:
- Every sent message got a unique Message-ID (see mailer.py).
- When someone replies, their mail client normally copies that ID into
  the reply's In-Reply-To and/or References headers.
- This script logs into the mailbox a message was actually sent from,
  scans recent messages, and checks whether their In-Reply-To/References
  contain a Message-ID we're waiting on. If so, and the reply's From
  address matches the original recipient, it's recorded as a reply.
- Outreach leads (sent from OUTREACH_SMTP_USERNAME, e.g.
  mairablogwrites@gmail.com) get replies in THAT mailbox, not the main
  one — so when OUTREACH_IMAP_HOST is configured, this checks both
  inboxes, each against only the recipients that were actually sent
  from it.

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
    """None if this mailbox isn't configured at all (prefix is optional)."""
    def _get(name, default=None):
        if prefix:
            value = os.environ.get(prefix + name)
            if value is not None:
                return value
        return os.environ.get(name, default)

    host = _get("IMAP_HOST")
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


def check_for_replies() -> int:
    """Returns the number of newly-detected replies, across every configured mailbox."""
    outreach_cfg = _imap_config(prefix="OUTREACH_")

    # If outreach has its own mailbox, split recipients so each inbox is
    # only checked against the messages it could actually have received.
    main_pending = db.get_all_recipients_for_reply_check(exclude_kind="outreach" if outreach_cfg else None)
    found = _scan_mailbox(_imap_config(), main_pending)

    if outreach_cfg:
        found += _scan_mailbox(outreach_cfg, db.get_all_recipients_for_reply_check(only_kind="outreach"))

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
