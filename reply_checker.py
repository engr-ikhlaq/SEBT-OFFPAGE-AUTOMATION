"""
reply_checker.py
Optional, best-effort reply detection over IMAP.

How it works:
- Every sent message got a unique Message-ID (see mailer.py).
- When someone replies, their mail client normally copies that ID into
  the reply's In-Reply-To and/or References headers.
- This script logs into the same mailbox you send from (or any inbox you
  configure), scans recent messages, and checks whether their
  In-Reply-To/References contain a Message-ID we're waiting on. If so,
  and the reply's From address matches the original recipient, it's
  recorded as a reply in the database.

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


def _imap_config():
    return {
        "host": os.environ["IMAP_HOST"],
        "port": int(os.environ.get("IMAP_PORT", 993)),
        "username": os.environ.get("IMAP_USERNAME", os.environ.get("SMTP_USERNAME")),
        "password": os.environ.get("IMAP_PASSWORD", os.environ.get("SMTP_PASSWORD")),
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
    """Returns the number of newly-detected replies."""
    cfg = _imap_config()

    pending = db.get_all_recipients_for_reply_check()
    if not pending:
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
