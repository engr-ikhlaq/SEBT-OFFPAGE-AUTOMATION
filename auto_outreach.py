"""
auto_outreach.py
Turns confirmed leads — from whichever store belongs to whoever clicked
the button, see data_source.for_user() — into sent guest-post pitches,
with no manual compose step.

Sending identity: the caller provides it (cfg, sender) — normally
app.sender_for_current_user(), so the pitch goes out as whoever is logged
in and clicked "Send to new leads now": their connected Google account if
they have one (Gmail API, no app password), otherwise the shared SMTP_*
config in .env. This module has no fixed persona of its own.

Lead data: the caller also provides `backend` (data_source.for_user()'s
result) — the SAME person's own leads, not a shared pool. Two different
users calling send_pending_leads() never touch each other's data.

send_pending_leads():
    - Reads rows with an empty Status from `backend` (already skips rows
      marked "sent").
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
import random

import db
from mailer import MailerConfig, personalize, send_batch

log = logging.getLogger("auto_outreach")

FOLLOW_UP_AFTER_HOURS = 24

# Several independent variants per slot, recombined per lead (see
# _build_outreach_message) - sending the exact same template to dozens of
# different recipients is itself a spam signal (near-duplicate bulk
# content is one of the things filters look for), separately from
# whatever the content actually says. {{url}}, {{keyword}}, {{sender_name}}
# are substituted per lead by mailer.personalize(); {{email}}/{{token}}/
# {{pixel_url}} come from the existing tracking machinery.

_SUBJECT_VARIANTS = (
    "Content Contribution Idea - {{domain}}",
    "A quick content idea for {{domain}}",
    "Pitching a piece for {{domain}}",
)

# Each entry is (greeting, first_paragraph) - two separate lines/paragraphs.
_OPENING_VARIANTS = (
    ("Hi,", "I came across this page: {{url}} — and wanted to reach out about contributing something related."),
    ("Hi,", "I landed on {{url}} recently and wanted to reach out — I'd love to contribute something to your site."),
    ("Hello,", "I've been reading through {{url}} and wanted to get in touch about a possible contribution."),
)

_PITCH_INTRO_VARIANTS = (
    "I write original, data-backed content and think a related piece could be a good fit for your site. "
    "A few directions I could take it:",
    "I focus on well-researched, original writing and think something adjacent could work well for your "
    "readers. A couple of directions that could work:",
    "My writing tends to be research-driven and original. Here are a few angles that might suit your audience:",
)

_BULLET_LINES = (
    "- A survey or data-driven piece (related to {{keyword}})",
    "- A myth-vs-fact or comparison breakdown of common approaches",
    "- A practical guide or checklist",
)

_CLOSER_VARIANTS = (
    "Do you have any recommendations on what would be most useful for your readers — whether that's one of "
    "the above, a different angle to research or compare, or something else relevant to {{keyword}}?",
    "Would any of these be useful to your readers, or is there a different angle on {{keyword}} you'd rather "
    "see covered?",
    "Let me know if any of these would be a good fit, or if there's something else about {{keyword}} you'd "
    "find more useful.",
)

_APPRECIATION_VARIANTS = (
    "Happy to send a short outline first so your team can review before anything's finalized — and if it "
    "runs, a citation/link back to my source would be appreciated.",
    "I can send over a short outline first if that's easier to review — and a credit/link back to my source "
    "would be appreciated if it goes live.",
    "I'm glad to share a brief outline before writing anything in full, and would appreciate a link back to "
    "my source if it's published.",
)

_SIGNOFF_VARIANTS = ("Regards,", "Best,", "Thanks,")


def _build_outreach_message() -> tuple[str, tuple[str, ...]]:
    """A fresh (subject, body_lines) combination, picked independently per
    call — so two different leads essentially never get byte-identical
    content, without changing what the pitch actually says."""
    greeting, opening = random.choice(_OPENING_VARIANTS)
    lines = (
        greeting,
        "",
        opening,
        "",
        random.choice(_PITCH_INTRO_VARIANTS),
        "",
        *_BULLET_LINES,
        "",
        random.choice(_CLOSER_VARIANTS),
        "",
        random.choice(_APPRECIATION_VARIANTS),
        "",
        random.choice(_SIGNOFF_VARIANTS),
        "{{sender_name}}",
    )
    return random.choice(_SUBJECT_VARIANTS), lines


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
    """Blank entries in `lines` are paragraph SEPARATORS, not content - they
    used to also render as a literal <br>, stacking on top of each <p>'s own
    margin and doubling the visual gap between paragraphs. Skipped here, and
    every real line gets one explicit margin instead of relying on each
    email client's own (inconsistent) default <p> spacing."""
    return "".join(f'<p style="margin:0 0 1em;">{line}</p>' for line in lines if line)


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


def send_pending_leads(base_url: str, cfg: MailerConfig, sender, backend, limit: int | None = None,
                        on_progress=None, should_stop=None) -> dict:
    """Sends the outreach email to every unsent, valid-email lead in
    `backend` — the caller's own data_source.for_user() result, so this
    only ever touches their own leads.

    cfg/sender: the sending identity to use — see module docstring.
    on_progress: optional callback(done, total, email, status) after each
        lead is processed — status is "sent", "failed", or "skipped". Lets
        a caller (e.g. outreach_job.py) report live progress for a run
        that can take minutes (one email at a time, throttled).
    should_stop: optional callback() -> bool, checked before each lead;
        if it returns True, the run stops there — whatever was already
        sent stays sent, same as the scrape job's Stop button.
    Returns {"sent": [...], "failed": [...], "skipped": [...]}.
    """
    from link_paths import click_url, open_pixel_url, unsubscribe_url as build_unsub_url

    leads = backend.read_recipients(skip_already_sent=True)
    if limit is not None:
        leads = leads[:limit]

    summary = {"sent": [], "failed": [], "skipped": []}

    for done, lead in enumerate(leads, start=1):
        if should_stop is not None and should_stop():
            break

        email = lead.get("Email", "").strip()
        domain = lead.get("Domain", "").strip()
        url = lead.get("URL", "").strip() or f"https://{domain}"
        keyword = lead.get("Keyword", "").strip() or "this topic"
        row = lead["row"]

        if not email:
            summary["skipped"].append(row)
            if on_progress is not None:
                on_progress(done, len(leads), "", "skipped")
            continue

        subject_template, body_lines = _build_outreach_message()

        campaign_id = db.create_campaign(
            subject=personalize(subject_template, {"domain": domain}),
            message="(auto-outreach)", link_url=url, link_text="",
        )
        _, token = db.create_recipient(
            campaign_id, email, kind="outreach", sheet_row=row, lead_domain=domain, lead_url=url,
            sender_email=cfg.from_email,
        )

        pixel_url_template = open_pixel_url(base_url, "{{token}}")
        unsubscribe_url_template = build_unsub_url(base_url, "{{email}}")
        html_template = _render_email_html(
            _html_body(body_lines),
            pixel_url_template=pixel_url_template,
            unsubscribe_url_template=unsubscribe_url_template,
        )
        text_template = _text_body(body_lines) + f"\n\nUnsubscribe: {unsubscribe_url_template}"

        extra = {email: {"domain": domain, "url": url, "keyword": keyword, "sender_name": cfg.from_name}}

        try:
            result = send_batch(
                cfg,
                sender,
                recipients=[email],
                subject=subject_template,
                html_template=html_template,
                text_template=text_template,
                unsubscribe_url_template=unsubscribe_url_template,
                tokens={email: token},
                extra_fields=extra,
            )
        except Exception as exc:  # SMTP/connection failure for this one lead
            log.exception("Send failed for %s", email)
            db.mark_recipient_result(email, campaign_id, "failed", error=str(exc))
            backend.write_result(row, status="failed", error=str(exc))
            summary["failed"].append((email, str(exc)))
            if on_progress is not None:
                on_progress(done, len(leads), email, "failed")
            continue

        if result["sent"]:
            message_id = result["message_ids"][email]
            db.mark_recipient_result(email, campaign_id, "sent", message_id=message_id)
            backend.write_result(row, status="sent")
            summary["sent"].append(email)
            log.info("Outreach sent to %s (%s)", email, domain)
            if on_progress is not None:
                on_progress(done, len(leads), email, "sent")
        else:
            _, err = result["failed"][0]
            db.mark_recipient_result(email, campaign_id, "failed", error=err)
            backend.write_result(row, status="failed", error=err)
            summary["failed"].append((email, err))
            if on_progress is not None:
                on_progress(done, len(leads), email, "failed")

    return summary


def send_due_followups(base_url: str, cfg: MailerConfig, sender, hours: int = FOLLOW_UP_AFTER_HOURS) -> dict:
    """Sends one follow-up to each outreach lead that's gone unanswered for `hours`.

    cfg/sender: the sending identity to use — see module docstring.
    """
    from link_paths import open_pixel_url, unsubscribe_url as build_unsub_url

    due = db.get_recipients_due_for_followup(hours=hours)
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
            sender_email=cfg.from_email,
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
                sender,
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
