"""
app.py
Flask front-end for the mailer.

Security notes:
- The send form is protected by a simple password gate (ADMIN_PASSWORD env
  var) + Flask session, so random visitors can't use your SMTP account to
  spam through your app. For anything beyond personal use, replace this
  with real auth (Flask-Login, OAuth, etc.).
- CSRF protection via Flask-WTF on all POST forms.
- Recipient emails are validated and de-duplicated before sending.
- An unsubscribe link is baked into every email; anyone who clicks it is
  added to suppression_list.json and permanently skipped in future sends,
  even if you accidentally re-upload their address. Honoring unsubscribes
  quickly is both a legal requirement (CAN-SPAM/GDPR) and a big
  deliverability factor.

Dashboard / tracking notes:
- Every send creates a campaign row and one recipient row per address
  (db.py / tracking.db, a local SQLite file).
- Each recipient gets a unique token used for the open-pixel and
  click-redirect endpoints registered in tracking.py.
- /dashboard shows aggregate + per-campaign + per-recipient stats.
- Reply detection is best-effort and optional: /check-replies logs into
  the IMAP inbox (if IMAP_* env vars are set) and matches replies back to
  sent messages via the Message-ID / In-Reply-To headers. See
  reply_checker.py.
"""

import os
import json
import logging
import math
from pathlib import Path

from flask import Flask, request, render_template, redirect, url_for, session, flash
from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileAllowed
from flask_wtf.csrf import CSRFProtect
from wtforms import StringField, TextAreaField, PasswordField, BooleanField
from wtforms.validators import DataRequired, Optional
from email_validator import validate_email, EmailNotValidError
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash

import auto_outreach
import google_oauth
import scrape_job
from mailer import GmailApiSender, MailerConfig, SmtpSender, send_batch
from tracking import tracking_bp
import data_source
import db
import sheets_source

load_dotenv()
log = logging.getLogger("app")

# google-auth-oauthlib refuses to run the OAuth flow over plain http, since a
# real deployment must use https. Localhost testing is the documented
# exception — this is never set when PUBLIC_BASE_URL points at a real https
# domain, so production deployments still get the normal protection.
if not os.environ.get("PUBLIC_BASE_URL", "").startswith("https://"):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-key-change-me")
csrf = CSRFProtect(app)
app.register_blueprint(tracking_bp)

db.init_db()


def _seed_owner_account():
    """First run only: create the owner account from .env so there's a way to log in.

    After this, credentials live only as a hash in tracking.db — ADMIN_USERNAME/
    ADMIN_PASSWORD in .env are not read again once a user exists.
    """
    if db.count_users() > 0:
        return
    username = os.environ.get("ADMIN_USERNAME", "").strip()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not username or not password:
        raise RuntimeError(
            "No users exist yet and ADMIN_USERNAME/ADMIN_PASSWORD are not set in .env — "
            "set them once to create the first (owner) login, then they can be changed."
        )
    db.create_user(username, generate_password_hash(password), role="owner", created_by="setup")


_seed_owner_account()

SUPPRESSION_FILE = Path(__file__).parent / "suppression_list.json"


def load_suppression_list() -> set:
    if SUPPRESSION_FILE.exists():
        return set(json.loads(SUPPRESSION_FILE.read_text()))
    return set()


def save_suppression_list(emails: set):
    SUPPRESSION_FILE.write_text(json.dumps(sorted(emails), indent=2))


def get_mailer_config() -> MailerConfig:
    return MailerConfig.from_env()


def _display_name_for(username: str) -> str:
    """'ikhlaq-wahid' -> 'Ikhlaq Wahid' — used as the From name for a
    connected account, so each user signs as themselves rather than a
    shared static name."""
    return username.replace("-", " ").replace("_", " ").title() or username


def sender_for_current_user() -> tuple[MailerConfig, object]:
    """(cfg, sender) for whoever is logged in, checked in this order:
    1. Their connected Google account (OAuth, Gmail API, no app password).
    2. Their manually-connected Gmail (email + app password typed directly
       into the app — no admin OAuth setup needed, see /connect/gmail-manual).
    3. The shared SMTP_* config from .env — same as before either existed.
    Used for BOTH manual campaigns and outreach, so "the outreach identity"
    is simply whoever clicked the button."""
    username = session.get("username", "")

    google_account = db.get_google_account(username)
    if google_account:
        creds = google_oauth.credentials_from_row(google_account)
        if creds.token != google_account["access_token"]:
            db.update_google_access_token(username, creds.token)
        cfg = MailerConfig(
            host="", port=0, username=google_account["google_email"], password="",
            from_name=_display_name_for(username),
            from_email=google_account["google_email"],
            reply_to=google_account["google_email"],
            send_delay_seconds=os.environ.get("SEND_DELAY_SECONDS", 3),
            max_emails_per_run=os.environ.get("MAX_EMAILS_PER_RUN", 150),
        )
        return cfg, GmailApiSender(creds)

    manual_account = db.get_manual_email_account(username)
    if manual_account:
        cfg = MailerConfig(
            host="smtp.gmail.com", port=587,
            username=manual_account["email"], password=manual_account["app_password"],
            from_name=_display_name_for(username),
            from_email=manual_account["email"], reply_to=manual_account["email"],
            send_delay_seconds=os.environ.get("SEND_DELAY_SECONDS", 3),
            max_emails_per_run=os.environ.get("MAX_EMAILS_PER_RUN", 150),
        )
        return cfg, SmtpSender(cfg)

    cfg = get_mailer_config()
    return cfg, SmtpSender(cfg)


def _visible_leads(backend, username: str) -> list[dict]:
    """This user's leads, minus whatever "Clear leads" has hidden (see
    clear_leads()) — NEVER deletes anything from backend itself, just
    filters rows at or before their watermark out of what gets displayed."""
    watermark = db.get_leads_watermark(username)
    rows = backend.read_recipients(skip_already_sent=False)
    return [row for row in rows if row.get("row", 0) > watermark]


def _lead_counts(rows: list[dict]) -> dict:
    counts = {"total": 0, "sent": 0, "failed": 0, "pending": 0}
    for row in rows:
        status = str(row.get("Status") or "").strip().lower()
        counts["total"] += 1
        counts[status if status in ("sent", "failed") else "pending"] += 1
    return counts


def _google_account_context() -> dict:
    """Shared by every index.html render call, so the connect-status panel
    (and the fallback-to-SMTP note) always reflects the same lookup."""
    username = session.get("username", "")
    backend = data_source.for_user(username, require_owner())
    try:
        scraped_leads = _visible_leads(backend, username)
        scraped_leads.sort(key=lambda lead: lead.get("row", 0), reverse=True)
        scraped_leads = scraped_leads[:50]
    except Exception:
        log.exception("Could not read leads from %s", backend.name)
        scraped_leads = []
    return {
        "google_account": db.get_google_account(username),
        "manual_account": db.get_manual_email_account(username),
        "google_oauth_configured": google_oauth.is_configured(),
        "manual_gmail_form": ManualGmailForm(),
        "data_backend_name": backend.name,
        "scraped_leads": scraped_leads,
    }


def _any_imap_configured() -> bool:
    """True if either the main or the outreach mailbox has IMAP set up."""
    return bool(os.environ.get("IMAP_HOST") or os.environ.get("OUTREACH_IMAP_HOST"))


def _gauge_needle(pct: float, cx: float = 60, cy: float = 65, r: float = 50) -> tuple[float, float]:
    """(x, y) of the needle tip on the semicircle gauge, for a 0-1 fraction.

    The arc runs from (cx-r, cy) at pct=0, through the top (cx, cy-r) at
    pct=0.5, to (cx+r, cy) at pct=1 — matching the filled-arc stroke so the
    needle and the colored sweep always agree on which way is "more".
    """
    angle = math.pi * (1 - pct)
    return cx + r * math.cos(angle), cy - r * math.sin(angle)


def _sparkline_points(series: list[dict], days: int = 7, width: int = 120, height: int = 36) -> str:
    """SVG <polyline points="..."> for a day-count series, oldest first.

    Pads with zeros on the left so a brand-new app still draws a flat line
    instead of a single point, and never divides by zero when every count
    (or the whole series) is empty.
    """
    counts = [row["n"] for row in series[-days:]]
    counts = [0] * (days - len(counts)) + counts
    peak = max(counts) or 1
    step = width / max(len(counts) - 1, 1)
    points = [
        f"{i * step:.1f},{height - (count / peak) * (height - 4) - 2:.1f}"
        for i, count in enumerate(counts)
    ]
    return " ".join(points)


def get_public_base_url() -> str:
    """
    Base URL used to build tracking links embedded in outgoing emails.
    Must be publicly reachable by the recipient's mail client — localhost
    only works while testing locally. Set PUBLIC_BASE_URL in .env once you
    deploy (e.g. https://mailer.yourdomain.com).
    """
    configured = os.environ.get("PUBLIC_BASE_URL")
    if configured:
        return configured.rstrip("/")
    return request.host_url.rstrip("/")


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])


class AddUserForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])


class ManualGmailForm(FlaskForm):
    email = StringField("Gmail address", validators=[DataRequired()])
    app_password = PasswordField("App Password", validators=[DataRequired()])


class CampaignForm(FlaskForm):
    subject = StringField("Subject", validators=[DataRequired()])
    link_url = StringField("Link to include in the email", validators=[DataRequired()])
    link_text = StringField("Link display text", validators=[DataRequired()])
    message = TextAreaField("Message body", validators=[DataRequired()])
    recipients = TextAreaField("Recipients (one email per line)", validators=[Optional()])
    recipients_file = FileField(
        "Or upload a .txt/.csv file (one email per line, or one per row)",
        validators=[Optional(), FileAllowed(["txt", "csv"], "Only .txt or .csv files allowed")],
    )
    use_google_sheet = BooleanField("Pull recipients from connected Google Sheet", validators=[Optional()])


def require_login():
    return session.get("user_id") is not None


def require_owner():
    return session.get("role") == "owner"


@app.route("/login", methods=["GET", "POST"])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        user = db.get_user_by_username(form.username.data.strip())
        if user and check_password_hash(user["password_hash"], form.password.data):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            # First login (or hasn't connected a Gmail account yet, by
            # either method): walk them through it once, rather than
            # leaving them to find it buried on the Compose page.
            connected = db.get_google_account(user["username"]) or db.get_manual_email_account(user["username"])
            return redirect(url_for("onboarding" if not connected else "index"))
        flash("Incorrect username or password.")
    return render_template("login.html", form=form)


@app.route("/onboarding", methods=["GET", "POST"])
def onboarding():
    """Shown right after login until the user connects Gmail (by either
    method) or chooses to skip — not forced every time, just once."""
    if not require_login():
        return redirect(url_for("login"))
    if db.get_google_account(session["username"]) or db.get_manual_email_account(session["username"]):
        return redirect(url_for("index"))

    form = ManualGmailForm()
    if form.validate_on_submit():
        db.save_manual_email_account(session["username"], form.email.data.strip(), form.app_password.data)
        flash(f"Connected {form.email.data.strip()}.")
        return redirect(url_for("index"))

    return render_template(
        "onboarding.html",
        form=form,
        google_oauth_configured=google_oauth.is_configured(),
        is_owner=require_owner(),
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/users", methods=["GET", "POST"])
def users():
    """Visible to everyone logged in (so anyone can find the 'Leave access'
    option below) - but only the owner can add or remove *other* people."""
    if not require_login():
        return redirect(url_for("login"))

    form = AddUserForm()
    if require_owner() and form.validate_on_submit():
        username = form.username.data.strip()
        if db.get_user_by_username(username):
            flash(f"'{username}' already has an account.")
        else:
            db.create_user(
                username, generate_password_hash(form.password.data),
                role="member", created_by=session["username"],
            )
            flash(f"Added '{username}'. Share their password with them directly, not over chat/email.")
        return redirect(url_for("users"))

    return render_template(
        "users.html", form=form,
        users=db.list_users() if require_owner() else None,
        current_user_id=session["user_id"], is_owner=require_owner(),
    )


@app.route("/users/<int:user_id>/delete", methods=["POST"])
def delete_user(user_id):
    """Owner removing someone ELSE's access. For your own, see /users/leave."""
    if not require_login():
        return redirect(url_for("login"))
    if not require_owner():
        flash("Only the owner account can manage users.")
        return redirect(url_for("dashboard"))

    if user_id == session["user_id"]:
        flash("That's your own account — use 'Leave access' below instead.")
    elif not db.delete_user(user_id):
        flash("Can't remove the last remaining account.")
    else:
        flash("Access removed.")
    return redirect(url_for("users"))


@app.route("/users/leave", methods=["POST"])
def leave_access():
    """Self-service removal: any user (owner included) can give up their own
    access, without needing someone else to do it for them."""
    if not require_login():
        return redirect(url_for("login"))

    if not db.delete_user(session["user_id"]):
        flash("You're the only account — add someone else before leaving, or this app would lock everyone out.")
        return redirect(url_for("users"))

    session.clear()
    flash("You've left this app. Your access has been removed.")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Connect Google Account — one-click OAuth: send from your own Gmail (no
# app password), and a Sheet of your own (no manual service-account
# sharing). See google_oauth.py for how this actually works.
# ---------------------------------------------------------------------------

@app.route("/connect/google")
def connect_google():
    if not require_login():
        return redirect(url_for("login"))
    if not google_oauth.is_configured():
        flash("Google sign-in isn't set up yet — ask whoever runs this app to add "
              "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET to .env.")
        return redirect(url_for("index"))

    redirect_uri = url_for("connect_google_callback", _external=True)
    url, state = google_oauth.authorization_url(redirect_uri)
    session["google_oauth_state"] = state
    return redirect(url)


@app.route("/connect/google/callback")
def connect_google_callback():
    if not require_login():
        return redirect(url_for("login"))

    expected_state = session.pop("google_oauth_state", None)
    if not expected_state or request.args.get("state") != expected_state:
        flash("That Google sign-in link expired or was tampered with — try connecting again.")
        return redirect(url_for("index"))
    if request.args.get("error"):
        flash(f"Google sign-in was cancelled ({request.args['error']}).")
        return redirect(url_for("index"))

    try:
        redirect_uri = url_for("connect_google_callback", _external=True)
        credentials = google_oauth.exchange_code(redirect_uri, request.url)
        email = google_oauth.get_connected_email(credentials)
        sheet_id = google_oauth.get_or_create_sheet(credentials)
    except Exception:
        log.exception("Google connect failed")
        flash("Could not finish connecting your Google account. Please try again.")
        return redirect(url_for("index"))

    db.save_google_account(
        username=session["username"],
        google_email=email,
        refresh_token=credentials.refresh_token,
        access_token=credentials.token,
        sheet_id=sheet_id,
    )

    try:
        backend = data_source.for_user(session["username"], require_owner())
        moved = data_source.migrate_to_sheet(session["username"], backend)
    except Exception:
        log.exception("Could not migrate local leads into the newly connected Sheet")
        moved = 0

    flash(
        f"Connected {email} — you can now send from it, and it has its own lead sheet."
        + (f" Moved {moved} existing lead(s) into it." if moved else "")
    )
    return redirect(url_for("index"))


@app.route("/connect/google/disconnect", methods=["POST"])
def connect_google_disconnect():
    if not require_login():
        return redirect(url_for("login"))
    db.delete_google_account(session["username"])
    flash("Disconnected your Google account. Sending falls back to the shared SMTP setup.")
    return redirect(url_for("index"))


@app.route("/connect/gmail-manual", methods=["POST"])
def connect_gmail_manual():
    """The no-OAuth-setup-needed alternative — see db.save_manual_email_account."""
    if not require_login():
        return redirect(url_for("login"))
    form = ManualGmailForm()
    if form.validate_on_submit():
        db.save_manual_email_account(session["username"], form.email.data.strip(), form.app_password.data)
        flash(f"Connected {form.email.data.strip()}.")
    else:
        flash("Enter both your Gmail address and an App Password.")
    return redirect(url_for("index"))


@app.route("/connect/gmail-manual/disconnect", methods=["POST"])
def connect_gmail_manual_disconnect():
    if not require_login():
        return redirect(url_for("login"))
    db.delete_manual_email_account(session["username"])
    flash("Disconnected. Sending falls back to the shared SMTP setup.")
    return redirect(url_for("index"))


@app.route("/", methods=["GET", "POST"])
def index():
    if not require_login():
        return redirect(url_for("login"))

    form = CampaignForm()
    results = None

    if form.validate_on_submit():
        raw_lines = (form.recipients.data or "").splitlines()

        # Merge in any addresses from an uploaded .txt/.csv file.
        # Works for either a plain list (one email per line) or a CSV
        # where the email is one of the comma-separated values per row.
        uploaded = form.recipients_file.data
        if uploaded and uploaded.filename:
            content = uploaded.read().decode("utf-8", errors="ignore")
            for line in content.splitlines():
                for piece in line.split(","):
                    piece = piece.strip()
                    if piece:
                        raw_lines.append(piece)

        # Optionally pull additional recipients from a connected Google Sheet.
        # sheet_row_by_email remembers which sheet row each address came from,
        # so we can write Status/Sent Time/Error back to that exact row later.
        sheet_row_by_email = {}
        if form.use_google_sheet.data:
            if not sheets_source.is_configured():
                flash("Google Sheet isn't configured yet — set GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_SHEET_ID in .env.")
                return render_template("index.html", form=form, results=None, scrape_status=scrape_job.get_status(), **_google_account_context())
            try:
                sheet_recipients = sheets_source.read_recipients(skip_already_sent=True)
            except Exception as e:
                flash(f"Could not read Google Sheet: {e}")
                return render_template("index.html", form=form, results=None, scrape_status=scrape_job.get_status(), **_google_account_context())

            for rec in sheet_recipients:
                email = rec["Email"].strip()
                raw_lines.append(email)
                sheet_row_by_email[email.strip().lower()] = rec["row"]

        if not raw_lines:
            flash("Provide recipients by pasting them, uploading a file, or enabling the Google Sheet source.")
            return render_template("index.html", form=form, results=None, scrape_status=scrape_job.get_status(), **_google_account_context())

        candidates = {line.strip() for line in raw_lines if line.strip()}

        valid_emails = set()
        invalid_emails = []
        for addr in candidates:
            try:
                valid_emails.add(validate_email(addr).normalized)
            except EmailNotValidError:
                invalid_emails.append(addr)

        suppressed = load_suppression_list()
        to_send = sorted(valid_emails - suppressed)
        skipped_unsubscribed = sorted(valid_emails & suppressed)

        # --- create campaign + per-recipient tracking rows up front ---
        campaign_id = db.create_campaign(
            subject=form.subject.data,
            message=form.message.data,
            link_url=form.link_url.data,
            link_text=form.link_text.data,
        )
        tokens = {}
        for email in to_send:
            _, token = db.create_recipient(campaign_id, email)
            tokens[email] = token

        base_url = get_public_base_url()
        click_url_template = f"{base_url}/t/c/{{{{token}}}}"
        pixel_url_template = f"{base_url}/t/o/{{{{token}}}}.png"

        html_template = render_template(
            "email_template.html",
            message=form.message.data,
            link_url=click_url_template,
            link_text=form.link_text.data,
            pixel_url=pixel_url_template,
            unsubscribe_url=(url_for("unsubscribe", email="{{email}}", _external=True)),
        )
        text_template = (
            f"{form.message.data}\n\n"
            f"{form.link_text.data}: {click_url_template}\n\n"
            f"Unsubscribe: {url_for('unsubscribe', email='{{email}}', _external=True)}"
        )

        try:
            cfg, sender = sender_for_current_user()
            results = send_batch(
                cfg,
                sender,
                recipients=to_send,
                subject=form.subject.data,
                html_template=html_template,
                text_template=text_template,
                unsubscribe_url_template=url_for("unsubscribe", email="{{email}}", _external=True),
                tokens=tokens,
            )
            results["invalid"] = invalid_emails
            results["skipped_unsubscribed"] = skipped_unsubscribed

            for email in results["sent"]:
                db.mark_recipient_result(
                    email, campaign_id, "sent",
                    message_id=results["message_ids"].get(email),
                )
            for email, err in results["failed"]:
                db.mark_recipient_result(email, campaign_id, "failed", error=err)

            db.finalize_campaign_counts(
                campaign_id,
                sent=len(results["sent"]),
                failed=len(results["failed"]),
                invalid=len(invalid_emails),
                skipped=len(skipped_unsubscribed),
            )

            # Write Status / Sent Time / Error back to the sheet for any
            # recipients that were sourced from it.
            if sheet_row_by_email:
                for email in results["sent"]:
                    row = sheet_row_by_email.get(email.strip().lower())
                    if row:
                        sheets_source.write_result(row, status="sent")

                for email, err in results["failed"]:
                    row = sheet_row_by_email.get(email.strip().lower())
                    if row:
                        sheets_source.write_result(row, status="failed", error=err)

                for email in invalid_emails:
                    row = sheet_row_by_email.get(email.strip().lower())
                    if row:
                        sheets_source.write_result(row, status="invalid", error="Invalid email format")
        except Exception as e:
            flash(f"Send failed: {e}")

    return render_template("index.html", form=form, results=results, scrape_status=scrape_job.get_status(), **_google_account_context())


@app.route("/unsubscribe")
def unsubscribe():
    email = request.args.get("email", "").strip().lower()
    if email:
        suppressed = load_suppression_list()
        suppressed.add(email)
        save_suppression_list(suppressed)
        db.mark_unsubscribed(email)
    return render_template("unsubscribed.html", email=email)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route("/dashboard")
def dashboard():
    if not require_login():
        return redirect(url_for("login"))

    overview = db.get_overview_stats()
    campaigns = db.get_campaigns_summary()
    timeseries = db.get_events_timeseries()
    sent_timeseries = db.get_sent_timeseries()
    recent_activity = db.get_recent_activity()

    sent = overview.get("total_sent") or 0
    reply_rate = round((overview.get("total_replies") or 0) / sent * 100) if sent else 0

    opens_by_day = [row for row in timeseries if row["event_type"] == "open"]
    sent_spark = _sparkline_points(sent_timeseries)
    needle_x, needle_y = _gauge_needle(reply_rate / 100)
    opens_spark = _sparkline_points(opens_by_day)

    backend = data_source.for_user(session["username"], require_owner())
    try:
        lead_counts = _lead_counts(_visible_leads(backend, session["username"]))
    except Exception:
        log.exception("Could not read lead counts from %s", backend.name)
        lead_counts = None

    return render_template(
        "dashboard.html",
        overview=overview,
        campaigns=campaigns,
        sent_spark=sent_spark,
        opens_spark=opens_spark,
        timeseries=timeseries,
        sent_timeseries=sent_timeseries,
        reply_rate=reply_rate,
        needle_x=needle_x,
        needle_y=needle_y,
        recent_activity=recent_activity,
        imap_configured=_any_imap_configured(),
        lead_journeys=db.get_lead_journeys(),
        unseen_replies=db.get_unseen_reply_count(),
        lead_counts=lead_counts,
        data_backend_name=backend.name,
        is_owner=require_owner(),
    )


@app.route("/leads/clear", methods=["POST"])
def clear_leads():
    """Hides every lead CURRENTLY visible to the caller from their own
    dashboard/Compose view — raises their watermark (see
    db.set_leads_watermark) past every row that exists right now.

    This never touches the actual Sheet or local file: the real data in
    backend.name is untouched, and anything scraped from here on (a new
    row, with a higher row number) still shows up normally."""
    if not require_login():
        return redirect(url_for("login"))

    backend = data_source.for_user(session["username"], require_owner())
    try:
        rows = backend.read_recipients(skip_already_sent=False)
        highest_row = max((row.get("row", 0) for row in rows), default=0)
        current_watermark = db.get_leads_watermark(session["username"])
        db.set_leads_watermark(session["username"], max(highest_row, current_watermark))
        flash(f"Cleared {len(rows)} lead(s) from your dashboard view. "
              f"{backend.name} itself is untouched — new leads will still show up.")
    except Exception as e:
        flash(f"Could not clear leads: {e}")

    return redirect(request.referrer or url_for("dashboard"))


@app.route("/leads/recent")
def leads_recent():
    """JSON list of the caller's own scraped leads (minus anything they've
    cleared from view) — polled from the Compose page so leads show up
    there as the scraper finds them, not only after a full page reload."""
    if (refusal := _require_login_json()) is not None:
        return refusal
    backend = data_source.for_user(session["username"], require_owner())
    try:
        leads = _visible_leads(backend, session["username"])
    except Exception as e:
        return {"error": str(e)}, 500
    leads.sort(key=lambda lead: lead.get("row", 0), reverse=True)
    return {"leads": leads[:50], "backend_name": backend.name}


@app.route("/dashboard/replies-seen", methods=["POST"])
def mark_replies_seen():
    if not require_login():
        return redirect(url_for("login"))
    db.mark_all_replies_seen()
    return redirect(url_for("dashboard"))


@app.route("/outreach/run", methods=["POST"])
def run_outreach():
    if not require_login():
        return redirect(url_for("login"))

    try:
        cfg, sender = sender_for_current_user()
        backend = data_source.for_user(session["username"], require_owner())
        result = auto_outreach.send_pending_leads(get_public_base_url(), cfg, sender, backend)
        flash(
            f"Outreach run complete — sent {len(result['sent'])}, "
            f"failed {len(result['failed'])}, skipped {len(result['skipped'])}."
        )
    except Exception as e:
        flash(f"Outreach run failed: {e}")

    return redirect(url_for("dashboard"))


@app.route("/outreach/followups", methods=["POST"])
def run_followups():
    if not require_login():
        return redirect(url_for("login"))

    try:
        cfg, sender = sender_for_current_user()
        result = auto_outreach.send_due_followups(get_public_base_url(), cfg, sender)
        flash(f"Follow-ups complete — sent {len(result['sent'])}, failed {len(result['failed'])}.")
    except Exception as e:
        flash(f"Follow-up run failed: {e}")

    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Lead scraping (JSON API, driven by fetch() from index.html — a real scrape
# runs for a while, so these never block a page load; the page polls
# /scrape/status instead).
# ---------------------------------------------------------------------------

def _require_login_json():
    """JSON equivalent of require_login() — redirecting an AJAX call to a
    login page would just leave the fetch() holding useless HTML."""
    return None if require_login() else ({"error": "login required"}, 401)


@app.route("/scrape/status")
def scrape_status():
    if (refusal := _require_login_json()) is not None:
        return refusal
    return scrape_job.get_status()


@app.route("/scrape/start", methods=["POST"])
def scrape_start():
    if (refusal := _require_login_json()) is not None:
        return refusal
    keyword = (request.get_json(silent=True) or {}).get("keyword", "").strip()
    if not keyword:
        return {"error": "Enter a target keyword first."}, 400
    if not scrape_job.start(keyword, session["username"], require_owner()):
        return {"error": "A scrape is already running."}, 409
    return scrape_job.get_status()


@app.route("/scrape/continue", methods=["POST"])
def scrape_continue():
    if (refusal := _require_login_json()) is not None:
        return refusal
    if not scrape_job.continue_scraping():
        return {"error": "Nothing is waiting on a decision right now."}, 409
    return scrape_job.get_status()


@app.route("/scrape/stop", methods=["POST"])
def scrape_stop():
    if (refusal := _require_login_json()) is not None:
        return refusal
    scrape_job.stop()
    return scrape_job.get_status()


@app.route("/scrape/send-now", methods=["POST"])
def scrape_send_now():
    """The "send emails instead" half of the after-batch decision."""
    if (refusal := _require_login_json()) is not None:
        return refusal
    try:
        cfg, sender = sender_for_current_user()
        backend = data_source.for_user(session["username"], require_owner())
        result = auto_outreach.send_pending_leads(get_public_base_url(), cfg, sender, backend)
    except Exception as exc:
        return {"error": str(exc)}, 500
    scrape_job.stop()  # don't let a later "continue" click resume after this
    return {
        "sent": len(result["sent"]),
        "failed": len(result["failed"]),
        "skipped": len(result["skipped"]),
    }


@app.route("/dashboard/campaign/<int:campaign_id>")
def campaign_detail(campaign_id):
    if not require_login():
        return redirect(url_for("login"))

    detail = db.get_campaign_detail(campaign_id)
    if not detail:
        flash("Campaign not found.")
        return redirect(url_for("dashboard"))

    return render_template("campaign_detail.html", **detail)


@app.route("/check-replies", methods=["POST"])
def check_replies():
    if not require_login():
        return redirect(url_for("login"))

    if not _any_imap_configured():
        flash("IMAP_HOST is not set in .env — reply checking is not configured.")
        return redirect(url_for("dashboard"))

    from reply_checker import check_for_replies
    try:
        found = check_for_replies()
        flash(f"Reply check complete — {found} new repl{'y' if found == 1 else 'ies'} found.")
    except Exception as e:
        flash(f"Reply check failed: {e}")

    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    # debug=False in anything resembling production
    app.run(debug=True, port=5000)
