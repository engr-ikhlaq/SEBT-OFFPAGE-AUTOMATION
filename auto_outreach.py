"""
auto_outreach.py
Turns confirmed leads from the connected Google Sheet into sent guest-post
pitches, with no manual compose step.

send_pending_leads():
    - Reads rows from the sheet whose Status is still empty (sheets_source
      already skips rows marked "sent").
    - Sends each one individually through mailer.send_batch (so it gets the
      same throttling, List-Unsubscribe header, and tracking as every other
      email this app sends).
    - Writes Status/Sent Time/Error back to that row, and records it in
      tracking.db, immediately after each send — not batched — so the
      sheet and dashboard always reflect exactly how far a run has gotten,
      even if it's interrupted partway through.

send_due_followups():
    - Finds outreach leads sent more than FOLLOW_UP_AFTER_HOURS ago with no
      reply and no follow-up yet, and sends one short follow-up, threaded
      under the original message (In-Reply-To), then marks it followed up.

Both are safe to call repeatedly (e.g. from a scheduled task) — already-sent
or already-followed-up rows are skipped automatically.
"""

from __future__ import annotations

import logging

import db
import sheets_source
from mailer import MailerConfig, SmtpSender, personalize, send_batch

log = logging.getLogger("auto_outreach")

FOLLOW_UP_AFTER_HOURS = 24


def _outreach_mailer_config() -> MailerConfig:
    """The guest-post pitch's own sending identity (e.g. mairablogwrites@gmail.com),
    separate from whatever account sends everything else. Only the OUTREACH_*
    values that actually differ need to be set — anything left unset falls
    back to the main SMTP_*/FROM_* config (see MailerConfig.from_env)."""
    return MailerConfig.from_env(prefix="OUTREACH_")

OUTREACH_SUBJECT = "Content Contribution Idea - {{domain}}"

# {{url}}, {{keyword}}, {{sender_name}} are substituted per lead by mailer.personalize().
# {{email}}/{{token}}/{{pixel_url}} come from the existing tracking machinery.
_OUTREACH_BODY_LINES = (
    "Hi,",
    "",
    "I came across this page: {{url}} — and wanted to reach out about "
    "contributing something related.",
    "",
    "I write original, data-backed content and think a related piece could "
    "be a good fit for your site. A few directions I could take it:",
    "",
    "- A survey or data-driven piece (related to {{keyword}})",
    "- A myth-vs-fact or comparison breakdown of common approaches",
    "- A practical guide or checklist",
    "",
    "Do you have any recommendations on what would be most useful for your "
    "readers — whether that's one of the above, a different angle to "
    "research or compare, or something else relevant to {{keyword}}?",
    "",
    "Happy to send a short outline first so your team can review before "
    "anything's finalized — and if it runs, a citation/link back to my "
    "source would be appreciated.",
    "",
    "Regards,",
    "{{sender_name}}",
)

FOLLOWUP_SUBJECT = "Re: Content Contribution Idea - {{domain}}"

_FOLLOWUP_BODY_LINES = (
    "Hi again,",
    "",
    "Just following up on my note below in case it got buried — happy to "
    "send a short outline for {{keyword}} if useful, or feel free to let me "
    "know if now isn't a good time.",
    "",
    "Regards,",
    "{{sender_name}}",
)


def _html_body(lines: tuple[str, ...]) -> str:
    return "".join(f"<p>{line}</p>" if line else "<br>" for line in lines)


def _text_body(lines: tuple[str, ...]) -> str:
    return "\n".join(lines)


def _render_email_html(body_html: str, *, pixel_url_template: str, unsubscribe_url_template: str) -> str:
    from flask import render_template  # imported lazily: needs an app context
    return render_template(
        "outreach_email.html",
        message=body_html,
        pixel_url=pixel_url_template,
        unsubscribe_url=unsubscribe_url_template,
    )


def send_pending_leads(base_url: str, limit: int | None = None) -> dict:
    """Sends the outreach email to every unsent, valid-email lead in the sheet.

    Returns {"sent": [...], "failed": [...], "skipped": [...]}.
    """
    if not sheets_source.is_configured():
        raise RuntimeError("Google Sheet isn't configured — set GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_SHEET_ID.")

    from link_paths import click_url, open_pixel_url, unsubscribe_url as build_unsub_url

    leads = sheets_source.read_recipients(skip_already_sent=True)
    if limit is not None:
        leads = leads[:limit]

    cfg = _outreach_mailer_config()
    summary = {"sent": [], "failed": [], "skipped": []}

    for lead in leads:
        email = lead.get("Email", "").strip()
        domain = lead.get("Domain", "").strip()
        url = lead.get("URL", "").strip() or f"https://{domain}"
        keyword = lead.get("Keyword", "").strip() or "this topic"
        row = lead["row"]

        if not email:
            summary["skipped"].append(row)
            continue

        campaign_id = db.create_campaign(
            subject=personalize(OUTREACH_SUBJECT, {"domain": domain}),
            message="(auto-outreach)", link_url=url, link_text="",
        )
        _, token = db.create_recipient(
            campaign_id, email, kind="outreach", sheet_row=row, lead_domain=domain, lead_url=url,
        )

        pixel_url_template = open_pixel_url(base_url, "{{token}}")
        unsubscribe_url_template = build_unsub_url(base_url, "{{email}}")
        html_template = _render_email_html(
            _html_body(_OUTREACH_BODY_LINES),
            pixel_url_template=pixel_url_template,
            unsubscribe_url_template=unsubscribe_url_template,
        )
        text_template = _text_body(_OUTREACH_BODY_LINES) + f"\n\nUnsubscribe: {unsubscribe_url_template}"

        extra = {email: {"domain": domain, "url": url, "keyword": keyword, "sender_name": cfg.from_name}}

        try:
            result = send_batch(
                cfg,
                SmtpSender(cfg),
                recipients=[email],
                subject=OUTREACH_SUBJECT,
                html_template=html_template,
                text_template=text_template,
                unsubscribe_url_template=unsubscribe_url_template,
                tokens={email: token},
                extra_fields=extra,
            )
        except Exception as exc:  # SMTP/connection failure for this one lead
            log.exception("Send failed for %s", email)
            db.mark_recipient_result(email, campaign_id, "failed", error=str(exc))
            sheets_source.write_result(row, status="failed", error=str(exc))
            summary["failed"].append((email, str(exc)))
            continue

        if result["sent"]:
            message_id = result["message_ids"][email]
            db.mark_recipient_result(email, campaign_id, "sent", message_id=message_id)
            sheets_source.write_result(row, status="sent")
            summary["sent"].append(email)
            log.info("Outreach sent to %s (%s)", email, domain)
        else:
            _, err = result["failed"][0]
            db.mark_recipient_result(email, campaign_id, "failed", error=err)
            sheets_source.write_result(row, status="failed", error=err)
            summary["failed"].append((email, err))

    return summary


def send_due_followups(base_url: str, hours: int = FOLLOW_UP_AFTER_HOURS) -> dict:
    """Sends one follow-up to each outreach lead that's gone unanswered for `hours`."""
    from link_paths import open_pixel_url, unsubscribe_url as build_unsub_url

    due = db.get_recipients_due_for_followup(hours=hours)
    cfg = _outreach_mailer_config()
    summary = {"sent": [], "failed": []}

    for recipient in due:
        email = recipient["email"]
        domain = recipient["lead_domain"] or ""
        keyword = "this topic"  # not stored per-recipient; good enough for a short nudge

        # A follow-up gets its own tracking token but stays linked to the
        # original lead via campaign_id, so the dashboard shows one journey.
        token = db.create_recipient(
            recipient["campaign_id"], email, kind="outreach",
            lead_domain=domain, lead_url=recipient.get("lead_url"),
        )[1]

        pixel_url_template = open_pixel_url(base_url, "{{token}}")
        unsubscribe_url_template = build_unsub_url(base_url, "{{email}}")
        html_template = _render_email_html(
            _html_body(_FOLLOWUP_BODY_LINES),
            pixel_url_template=pixel_url_template,
            unsubscribe_url_template=unsubscribe_url_template,
        )
        text_template = _text_body(_FOLLOWUP_BODY_LINES) + f"\n\nUnsubscribe: {unsubscribe_url_template}"
        extra = {email: {"domain": domain, "keyword": keyword, "sender_name": cfg.from_name}}

        try:
            result = send_batch(
                cfg,
                SmtpSender(cfg),
                recipients=[email],
                subject=FOLLOWUP_SUBJECT,
                html_template=html_template,
                text_template=text_template,
                unsubscribe_url_template=unsubscribe_url_template,
                tokens={email: token},
                extra_fields=extra,
                in_reply_to={email: recipient["message_id"]},
            )
        except Exception as exc:
            log.exception("Follow-up failed for %s", email)
            db.mark_recipient_result(email, recipient["campaign_id"], "failed", error=str(exc))
            summary["failed"].append((email, str(exc)))
            continue

        db.mark_followed_up(recipient["id"])
        if result["sent"]:
            db.mark_recipient_result(email, recipient["campaign_id"], "sent", message_id=result["message_ids"][email])
            summary["sent"].append(email)
            log.info("Follow-up sent to %s", email)
        else:
            _, err = result["failed"][0]
            db.mark_recipient_result(email, recipient["campaign_id"], "failed", error=err)
            summary["failed"].append((email, err))

    return summary
