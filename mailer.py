"""
mailer.py
Core email-sending logic.

Design choices that matter for deliverability:
- Each recipient gets ONE individual message (their address in "To"),
  never a giant To/CC/BCC list. Mass "To" lists are a classic spam signal
  and also leak everyone's address to everyone else.
- We reuse a single SMTP connection for the whole batch (faster, fewer
  handshakes look less "bot-like" than reconnecting each time) but we
  still pause SEND_DELAY_SECONDS between sends to avoid tripping rate
  limits or looking like a blast.
- Every message includes a real List-Unsubscribe header + a visible
  unsubscribe link in the body. This is one of the single biggest
  factors mailbox providers use to decide "wanted mail" vs "spam."
- Plain-text alternative is included alongside HTML. Mail with no
  plain-text part is a common spam trigger.
- Subject/body content is passed in by you — avoid ALL CAPS, excessive
  exclamation marks, and words like "free", "guarantee", "act now" etc.,
  which are classic spam-filter triggers.

Tracking:
- Each recipient has a unique token (see db.py). html_template /
  text_template may contain the literal placeholders "{{email}}" and
  "{{token}}", which get substituted per recipient before sending.
- A Message-ID is generated for every outgoing message and returned in
  the results so it can be stored and later matched against IMAP replies
  (see reply_checker.py).
"""

import os
import smtplib
import ssl
import time
import logging
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("mailer")


class MailerConfig:
    def __init__(self, host, port, username, password, from_name, from_email,
                 reply_to, send_delay_seconds=3, max_emails_per_run=150):
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.from_name = from_name
        self.from_email = from_email
        self.reply_to = reply_to
        self.send_delay_seconds = float(send_delay_seconds)
        self.max_emails_per_run = int(max_emails_per_run)

    @classmethod
    def from_env(cls, prefix: str = "") -> "MailerConfig":
        """The one place that reads SMTP_* / FROM_* / SEND_* out of the environment.

        With a prefix (e.g. "OUTREACH_"), each name is looked up as
        "<prefix><name>" first and falls back to the unprefixed name — so a
        second sending identity (a whole separate mailbox, not just a
        display name) only needs to set the values that actually differ.
        """
        def _get(name: str, default: str | None = None) -> str:
            if prefix:
                value = os.environ.get(prefix + name)
                if value is not None:
                    return value
            value = os.environ.get(name, default)
            if value is None:
                missing = f"{prefix}{name}" + (f" (or {name})" if prefix else "")
                raise KeyError(f"{missing} is not set")
            return value

        from_email = _get("FROM_EMAIL")
        return cls(
            host=_get("SMTP_HOST"),
            port=_get("SMTP_PORT"),
            username=_get("SMTP_USERNAME"),
            password=_get("SMTP_PASSWORD"),
            from_name=_get("FROM_NAME", ""),
            from_email=from_email,
            reply_to=_get("REPLY_TO_EMAIL", from_email),
            send_delay_seconds=_get("SEND_DELAY_SECONDS", "3"),
            max_emails_per_run=_get("MAX_EMAILS_PER_RUN", "150"),
        )


def personalize(template: str, fields: dict[str, str]) -> str:
    """Replace every "{{key}}" in template with fields[key]. Unknown keys are left as-is."""
    out = template
    for key, value in fields.items():
        out = out.replace("{{" + key + "}}", value)
    return out


def build_message(cfg: MailerConfig, to_email: str, subject: str,
                   html_body: str, text_body: str, unsubscribe_url: str,
                   in_reply_to: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg.from_name, cfg.from_email))
    msg["To"] = to_email
    msg["Reply-To"] = cfg.reply_to

    # Explicit Message-ID so replies (which echo it back via In-Reply-To /
    # References headers) can be matched to this exact send.
    msg["Message-ID"] = make_msgid(domain=cfg.from_email.split("@")[-1])

    # Threads a follow-up under the original message in the recipient's client.
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    # Real unsubscribe header — supported by Gmail/Outlook and boosts
    # "legitimate sender" signals a lot.
    msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    msg.set_content(text_body)  # plain-text fallback (required, not optional)
    msg.add_alternative(html_body, subtype="html")
    return msg


def send_batch(cfg: MailerConfig, recipients: list[str], subject: str,
               html_template: str, text_template: str,
               unsubscribe_url_template: str, tokens: dict[str, str],
               extra_fields: dict[str, dict[str, str]] | None = None,
               in_reply_to: dict[str, str] | None = None,
               on_progress=None):
    """
    recipients: list of email addresses (already validated/deduped by caller)
    *_template: strings that may contain "{{email}}" and "{{token}}" as
        personalization tokens, plus any key from extra_fields
    tokens: dict mapping email -> unique tracking token (from db.create_recipient)
    extra_fields: optional {email: {placeholder_name: value}}, for callers
        with their own per-recipient personalization (e.g. auto_outreach.py
        substituting "{{domain}}"/"{{url}}" for each lead). Merged with
        email/token before substitution, so one sending path (with the same
        throttling, headers and tracking) serves every caller.
    in_reply_to: optional {email: message_id} — set this to thread a message
        (e.g. a follow-up) under an earlier one instead of starting a new one.
    on_progress: optional callback(sent_count, total, current_email, status)

    Returns dict: {
        "sent": [...], "failed": [(email, error), ...],
        "message_ids": {email: message_id, ...}
    }
    """
    if len(recipients) > cfg.max_emails_per_run:
        raise ValueError(
            f"Refusing to send {len(recipients)} emails in one run "
            f"(limit is {cfg.max_emails_per_run}). Split into smaller batches "
            f"across multiple days to protect your sender reputation."
        )

    results = {"sent": [], "failed": [], "message_ids": {}}
    context = ssl.create_default_context()

    with smtplib.SMTP(cfg.host, cfg.port) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(cfg.username, cfg.password)

        for i, to_email in enumerate(recipients, start=1):
            try:
                token = tokens[to_email]
                fields = {"email": to_email, "token": token}
                fields.update((extra_fields or {}).get(to_email, {}))

                msg = build_message(
                    cfg, to_email, personalize(subject, fields),
                    html_body=personalize(html_template, fields),
                    text_body=personalize(text_template, fields),
                    unsubscribe_url=personalize(unsubscribe_url_template, fields),
                    in_reply_to=(in_reply_to or {}).get(to_email),
                )
                server.send_message(msg)
                results["sent"].append(to_email)
                results["message_ids"][to_email] = msg["Message-ID"]
                logger.info("Sent to %s (%d/%d)", to_email, i, len(recipients))
                status = "sent"
            except Exception as e:
                results["failed"].append((to_email, str(e)))
                logger.error("Failed to send to %s: %s", to_email, e)
                status = "failed"

            if on_progress:
                on_progress(i, len(recipients), to_email, status)

            # Throttle — do NOT remove this. It's the main thing standing
            # between "normal sender" and "looks like a bot blast."
            if i < len(recipients):
                time.sleep(cfg.send_delay_seconds)

    return results
