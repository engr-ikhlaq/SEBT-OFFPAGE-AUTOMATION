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

## Connecting Gmail: two ways, shown right after login

Every user is walked through connecting their own Gmail the first time they
log in (`/onboarding`) — not forced, "Skip for now" is always there. Two
options:

- **Manual (always available, no admin setup)** — two text boxes: Gmail
  address and an [App
  Password](https://myaccount.google.com/apppasswords) (not the normal
  Gmail password; needs 2-Step Verification on first). Works immediately,
  for anyone, regardless of whether the one-click option below is set up.
- **One-click Google sign-in (OAuth)** — see below. Nicer (no password
  ever typed into this app, gets its own auto-created lead sheet) but
  needs the one-time Cloud Console setup from whoever runs the app.

If a user has both, the one-click connection is used. Either way, campaigns
and outreach they send go out as them, not a shared identity — and anyone
who hasn't connected either falls back to the shared `SMTP_*` config.

### Connect Google Account (one-click, via OAuth)

Any logged-in user can click **Connect Google Account** and sign in with
their own Gmail through Google's own consent screen. No password ever
passes through this app. They get:

- **Sending as themselves** — through the Gmail API, not SMTP, so there's
  no app password to generate or protect.
- **Their own lead sheet** — created automatically on first connect, with
  the right headers already in place, no manual service-account sharing.
- **One click to disconnect** — and they can also revoke access directly
  at https://myaccount.google.com/permissions any time.

This needs a one-time setup that only you (whoever runs this app) can do,
since it requires your own Google Cloud account:

1. Go to https://console.cloud.google.com/ and pick (or create) a project.
2. **APIs & Services → Library** — enable the **Gmail API**, **Google
   Sheets API**, and **Google Drive API**.
3. **APIs & Services → OAuth consent screen** — set it up as "External"
   (unless everyone who'll use this has a Workspace account on the same
   domain, in which case "Internal" is simpler). Add yourself and anyone
   else who'll connect an account as a **test user** — this avoids needing
   Google's full app-verification review, which is really only required
   for a public-facing product.
4. **APIs & Services → Credentials → Create Credentials → OAuth client
   ID** — Application type **Web application**. Add this to **Authorized
   redirect URIs**:
   ```
   http://localhost:5000/connect/google/callback
   ```
   (add your real domain's equivalent too, later, if you deploy this
   somewhere other than your own machine).
5. Copy the **Client ID** and **Client Secret** into `.env`:
   ```
   GOOGLE_OAUTH_CLIENT_ID=...
   GOOGLE_OAUTH_CLIENT_SECRET=...
   ```
6. Restart the app. The Compose page now shows a **Connect Google
   Account** button instead of the "not set up" message.

Until a test-user Google account is verified, Google shows an "unverified
app" warning on the consent screen — click **Advanced → Go to (app name)
(unsafe)** to continue. This is expected for an app only you and people you
add as test users will use; it goes away only through Google's formal
verification process, which matters for a public product, not a personal
or small-team tool like this one.

The shared `SMTP_*` setup from the section above still works and is used
as the fallback for anyone who hasn't connected their own account — nothing
breaks if you skip this section entirely.

## Where leads are stored: local file by default, a Sheet if you connect one

Scraped leads and outreach status (`Status`/`Sent Time`/`Error`) live in a
local `leads.xlsx` file by default — created automatically, no setup. Set
`GOOGLE_SHEET_ID` + `GOOGLE_SERVICE_ACCOUNT_FILE` in `.env` and the app
switches to that Sheet instead, for everyone, the next time it starts — the
scraper, the dashboard's lead-count numbers, and outreach's "who's unsent"
check all read from whichever one is active (`data_source.py` is the one
place that decides). Switching back just means clearing those two env vars.

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

- `app.py` — Flask routes: login, campaign form, dashboard, Google connect, scraping API, unsubscribe endpoint
- `mailer.py` — sending logic (SmtpSender / GmailApiSender), throttling, personalization, tracking pixel/click-link injection
- `google_oauth.py` — the "Connect Google Account" OAuth flow, Gmail-API sending, and each account's auto-created sheet
- `db.py` — SQLite storage: campaigns/recipients/events, users, connected Google accounts, aggregate queries
- `tracking.py` — open-pixel and click-redirect endpoints
- `reply_checker.py` — optional IMAP-based reply detection (main mailbox and/or a connected outreach mailbox)
- `sheets_source.py` — reads/writes a connected Google Sheet (recipients for a campaign; one of the two lead-data backends)
- `local_store.py` — the other lead-data backend: a local `leads.xlsx` file, used automatically when no Sheet is connected
- `data_source.py` — picks between the two above; everything else goes through this, not sheets_source/local_store directly
- `auto_outreach.py` — the automated guest-post pitch + 24h follow-up, sourced from whichever backend is active
- `scrape_job.py` — runs the lead scraper (`offpage/`) in a background thread, driven from the Compose page
- `offpage/` — the Google-search lead finder (keyword → relevance-checked, email-verified leads)
- `templates/` — login, onboarding, compose (campaign + scraping + Gmail connect), dashboard, users, campaign detail, unsubscribe page
- `static/app.css` — the one shared stylesheet every page uses
- `leads.xlsx` — auto-created; the local lead-data backend (see data_source.py above)
- `tracking.db` — auto-created SQLite file; all tracking/user/connected-account data
- `seen_domains.db` — auto-created; scraper's dedup memory, so re-running a keyword doesn't repeat work
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
