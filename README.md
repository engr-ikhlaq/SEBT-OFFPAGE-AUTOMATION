# Flask Mailer

A small, secure Flask app for sending a personalized email (with a link and
an unsubscribe footer) to a list of recipients, throttled to protect your
sender reputation.

## Setup

```bash
cd flask-mailer
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Then edit `.env`:

1. **Gmail App Password** (do this even if you think you'll use another provider later):
   - Turn on 2-Step Verification: https://myaccount.google.com/security
   - Create an app password: https://myaccount.google.com/apppasswords
   - Put that 16-character password in `SMTP_PASSWORD` — never your real Gmail password.
2. Set `ADMIN_PASSWORD` and `FLASK_SECRET_KEY` to your own random values.

Run it:

```bash
python app.py
```

Visit `http://localhost:5000`, log in with `ADMIN_PASSWORD`, and fill out the form.

## Why this won't automatically land in spam (and what to still watch for)

Nothing guarantees inbox placement — spam filtering is entirely up to Gmail/
Outlook/etc. — but this app follows the practices that give you the best
realistic odds on a personal account:

- **One recipient per message.** No mass To/CC/BCC list.
- **Throttled sending** (`SEND_DELAY_SECONDS`) so you don't send in a burst
  that looks automated/abusive.
- **Real unsubscribe link + `List-Unsubscribe` header.** This is one of the
  strongest positive signals to mailbox providers.
- **Plain-text + HTML parts together.** HTML-only mail is a common spam flag.
- **A capped batch size** (`MAX_EMAILS_PER_RUN`) — send in smaller waves
  across multiple days rather than one huge blast, especially the first
  few times you use a given From address.
- **Content matters, not just code.** Avoid ALL CAPS, excessive "!!!", words
  like "free", "guarantee", "click now", and disguised/shortened links —
  these still get flagged by content-based filters no matter how clean your
  sending pattern is.
- **Warm up gradually.** If this address hasn't sent bulk mail before, start
  with 10–20 emails, see how they land, then scale up over days/weeks.

### The real ceiling on a personal account

Without a custom domain with SPF, DKIM, and DMARC records, you're relying
entirely on Gmail's own reputation for your address, and Gmail personal
accounts cap outgoing mail around **500/day** (Workspace accounts ~2000/day).
If you outgrow this or need reliable inbox placement at real volume, the
next step up is a transactional provider (SendGrid, Mailgun, Amazon SES,
Postmark) on your own domain with SPF/DKIM/DMARC configured — happy to build
that version too if you get a domain later.

## Dashboard & tracking

Every campaign you send now gets logged to a local SQLite file
(`tracking.db`, created automatically on first run) with per-recipient
tracking:

- **Sent / failed** — captured immediately from the SMTP send.
- **Opens** — a 1x1 tracking pixel is embedded in the HTML email; loading
  it logs an open. Mail-client image-loading behavior varies (Gmail/Outlook
  usually auto-load, Apple Mail Privacy Protection can pre-fetch and
  inflate counts) — treat open rate as a useful trend, not an exact number.
- **Clicks** — the link in the email is routed through a redirect that
  logs the click, then forwards to your real URL. Click tracking is
  generally far more reliable than open tracking.
- **Replies** (optional) — click "Check for replies" on the dashboard to
  scan an IMAP inbox for replies matched back to sent messages via the
  `Message-ID`/`In-Reply-To` headers. Requires `IMAP_*` vars in `.env`.
  This is best-effort, not a guarantee (see `reply_checker.py`).
- **Unsubscribes** — already tracked per recipient, now also shown per
  campaign on the dashboard.

Visit `/dashboard` (linked from the compose page once logged in) for:
overall stats, a 14-day opens/clicks trend chart, a per-campaign table
with open/click rate bars, a live recent-activity feed, and a per-campaign
detail page listing every recipient's individual status.

**Important:** the tracking pixel and click links are built from
`PUBLIC_BASE_URL` in `.env`. While testing on `localhost` this is ignored
in favor of the request's own host, but real recipients' mail clients
can't reach `localhost` — set `PUBLIC_BASE_URL` to your real deployed
domain before sending to anyone outside your own machine.

## Files

- `app.py` — Flask routes: login, campaign form, dashboard, unsubscribe endpoint
- `mailer.py` — SMTP sending logic, throttling, per-recipient personalization, tracking pixel/click-link injection
- `db.py` — SQLite storage + aggregate queries for the dashboard
- `tracking.py` — open-pixel and click-redirect endpoints
- `reply_checker.py` — optional IMAP-based reply detection
- `templates/` — login page, campaign form, HTML email body, dashboard, campaign detail, unsubscribe page
- `tracking.db` — auto-created SQLite file; all campaign/recipient/event tracking data
- `suppression_list.json` — auto-created; permanent record of unsubscribes,
  always excluded from future sends even if re-added to a recipient list
- `.env.example` — copy to `.env` and fill in real credentials (never commit `.env`)

## Legal note

Sending unsolicited bulk email is regulated (CAN-SPAM in the US, GDPR/PECR in
the EU/UK, similar laws elsewhere). At minimum: only email people who have a
real relationship with you or opted in, always honor unsubscribes promptly
(this app does automatically), and include your real identity/contact info
in the message. This isn't legal advice — check the rules for your
jurisdiction if you're sending anything commercial.
