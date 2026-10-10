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
import re
from pathlib import Path

import gspread
from flask import Flask, request, render_template, redirect, url_for, session, flash
from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileAllowed
from flask_wtf.csrf import CSRFProtect
from wtforms import StringField, TextAreaField, PasswordField, BooleanField
from wtforms.validators import DataRequired, Optional, Length, EqualTo
from email_validator import validate_email, EmailNotValidError
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash

import auto_outreach
import google_oauth
import outreach_job
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


def _recovery_email_for(username: str) -> str | None:
    """The address a password-reset link goes to: whichever Gmail this user
    has connected (OAuth or manual). Derived fresh each call rather than
    stored, so it can never drift out of sync with the connect/disconnect
    buttons on the Compose page. None until onboarding is complete — which,
    since connecting Gmail is required before using the app, is only the
    user's own very first login."""
    google_account = db.get_google_account(username)
    if google_account:
        return google_account["google_email"]
    manual_account = db.get_manual_email_account(username)
    if manual_account:
        return manual_account["email"]
    return None


def _send_password_reset_email(to_email: str, reset_url: str) -> None:
    """Sent while logged OUT, so this always goes through the shared
    SMTP_* config (.env) rather than sender_for_current_user() — there's no
    "current user" yet. A plain transactional message: no tracking pixel,
    no unsubscribe link, nothing that routes it through suppression_list.json
    (a password reset isn't marketing, and unsubscribing from campaigns
    shouldn't block someone from getting back into their own account)."""
    import smtplib
    import ssl
    from email.message import EmailMessage

    cfg = get_mailer_config()
    msg = EmailMessage()
    msg["Subject"] = "Reset your password — Guest Posting Automation"
    msg["From"] = cfg.from_email
    msg["To"] = to_email
    msg.set_content(
        f"A password reset was requested for your account.\n\n"
        f"Reset it here (expires in 1 hour): {reset_url}\n\n"
        f"If you didn't request this, you can ignore this email."
    )

    server = smtplib.SMTP(cfg.host, cfg.port)
    try:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        server.login(cfg.username, cfg.password)
        server.send_message(msg)
    finally:
        server.quit()


def _verify_gmail_app_password(email: str, app_password: str) -> None:
    """Confirms an (email, App Password) pair actually works by logging
    into Gmail's SMTP server with it — used at /signup and when resetting
    a Gmail-based account's password, so a typo or a copy-pasted "normal"
    password is caught immediately instead of failing silently the first
    time something tries to send. Raises on failure; the exception message
    is safe to flash as-is (smtplib's are already human-readable)."""
    import smtplib
    import ssl

    server = smtplib.SMTP("smtp.gmail.com", 587, timeout=15)
    try:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        server.login(email, app_password)
    finally:
        server.quit()


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


class _UnavailableBackend:
    """Stand-in used when even opening the real backend fails (e.g. a
    Sheets API quota/network hiccup) - gives the page something to render
    instead of a 500, see _google_account_context()."""
    name = "your leads store (temporarily unavailable)"


def _google_account_context() -> dict:
    """Shared by every index.html render call, so the connect-status panel
    (and the fallback-to-SMTP note) always reflects the same lookup."""
    username = session.get("username", "")
    try:
        backend = data_source.for_user(username, require_owner())
    except Exception:
        log.exception("Could not resolve a leads backend for %s", username)
        backend = _UnavailableBackend()

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
        "user_sheet": db.get_user_sheet(username),
        "service_account_sheets_configured": data_source.service_account_sheets_configured(),
        "service_account_email": sheets_source.service_account_email(),
        "connect_sheet_form": ConnectSheetForm(),
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


class ManualGmailForm(FlaskForm):
    email = StringField("Gmail address", validators=[DataRequired()])
    app_password = PasswordField("App Password", validators=[DataRequired()])


class ConnectSheetForm(FlaskForm):
    sheet_id = StringField("Sheet ID or URL", validators=[DataRequired()])
    worksheet_name = StringField("Worksheet name (optional)", validators=[Optional()])


def _extract_sheet_id(raw: str) -> str:
    """Accepts either a bare Sheet ID or a full Sheets URL (the part
    between /d/ and the next /) and returns just the ID either way, since
    pasting the whole URL from the address bar is the easier thing to do."""
    raw = raw.strip()
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", raw)
    return match.group(1) if match else raw


class ForgotPasswordForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired()])


class ResetPasswordForm(FlaskForm):
    password = PasswordField("New password", validators=[DataRequired(), Length(min=8)])
    confirm_password = PasswordField(
        "Confirm new password", validators=[DataRequired(), EqualTo("password", message="Passwords must match.")]
    )


class ResetGmailAppPasswordForm(FlaskForm):
    app_password = PasswordField("New App Password", validators=[DataRequired()])


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


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """Self-service account creation: a Gmail address + its App Password
    is both verified (live SMTP login) and immediately connected as the
    account's sending identity - one step instead of create-account-then-
    separately-onboard. That same App Password also becomes this user's
    login password (see login()'s check_password_hash), so there's exactly
    one secret to keep track of, not two."""
    if require_login():
        return redirect(url_for("index"))

    form = ManualGmailForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        app_password = form.app_password.data

        if db.get_user_by_username(email) or db.find_username_by_connected_email(email):
            flash(f"{email} already has an account — log in instead.")
            return redirect(url_for("login"))

        try:
            _verify_gmail_app_password(email, app_password)
        except Exception as e:
            flash(f"Could not verify that Gmail address and App Password: {e}")
            return render_template("signup.html", form=form)

        user_id = db.create_user(email, generate_password_hash(app_password), role="member", created_by="self-signup")
        db.save_manual_email_account(email, email, app_password)
        session["user_id"] = user_id
        session["username"] = email
        session["role"] = "member"
        flash(f"Welcome! {email} is connected and ready to go.")
        return redirect(url_for("index"))

    return render_template("signup.html", form=form)


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        username = form.username.data.strip()
        user = db.get_user_by_username(username)
        recovery_email = _recovery_email_for(username) if user else None
        if recovery_email:
            token = db.create_password_reset_token(username)
            reset_url = url_for("reset_password", token=token, _external=True)
            try:
                _send_password_reset_email(recovery_email, reset_url)
            except Exception:
                log.exception("Could not send password reset email for %s", username)
        # Same message either way — whether the username exists, and
        # whether it has a recovery email on file, is not something a
        # stranger submitting this form should be able to tell apart.
        flash("If that account has a connected Gmail on file, a reset link has been sent to it.")
        return redirect(url_for("login"))
    return render_template("forgot_password.html", form=form)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    token_row = db.get_valid_password_reset_token(token)
    if not token_row:
        flash("That reset link is invalid or has expired — request a new one.")
        return redirect(url_for("forgot_password"))

    username = token_row["username"]
    manual_account = db.get_manual_email_account(username)

    # A self-signed-up account's password IS its Gmail App Password (see
    # signup()) - resetting it to an arbitrary string would silently break
    # sending, so this path asks for a NEW App Password instead, verifies
    # it the same way signup does, and updates both records together.
    if manual_account:
        form = ResetGmailAppPasswordForm()
        if form.validate_on_submit():
            try:
                _verify_gmail_app_password(manual_account["email"], form.app_password.data)
            except Exception as e:
                flash(f"Could not verify that App Password: {e}")
                return render_template("reset_password.html", form=form, email=manual_account["email"])
            db.update_user_password(username, generate_password_hash(form.app_password.data))
            db.save_manual_email_account(username, manual_account["email"], form.app_password.data)
            db.mark_password_reset_token_used(token)
            flash("App Password updated — log in with it.")
            return redirect(url_for("login"))
        return render_template("reset_password.html", form=form, email=manual_account["email"])

    form = ResetPasswordForm()
    if form.validate_on_submit():
        db.update_user_password(username, generate_password_hash(form.password.data))
        db.mark_password_reset_token_used(token)
        flash("Password updated — log in with your new password.")
        return redirect(url_for("login"))
    return render_template("reset_password.html", form=form, email=None)


@app.route("/onboarding", methods=["GET", "POST"])
def onboarding():
    """Shown right after login, and enforced on every other page (see
    _require_gmail_connection below) until the user connects Gmail, by
    either method — there's no "skip" anymore, since every user needs
    their own sending identity before they can scrape or send anything."""
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


# Endpoints reachable before/without a connected Gmail account - account
# management, the connect flow itself, and anything a logged-out visitor
# hits (forgot/reset password, unsubscribe, tracking pixels/links).
_ONBOARDING_EXEMPT_ENDPOINTS = {
    "login", "signup", "logout", "onboarding", "forgot_password", "reset_password",
    "users", "delete_user", "leave_access",
    "connect_google", "connect_google_callback", "connect_google_disconnect",
    "connect_gmail_manual", "connect_gmail_manual_disconnect",
    "unsubscribe", "static", None,
}

# These return JSON (polled by fetch() from Compose), so a gated one needs
# a JSON error, not a redirect to a page the caller can't render.
_ONBOARDING_GATED_JSON_ENDPOINTS = {
    "scrape_status", "scrape_start", "scrape_continue", "scrape_stop",
    "scrape_send_now", "leads_recent",
}


@app.before_request
def _require_gmail_connection():
    """Connecting Gmail (OAuth or manual) is no longer optional — every
    user needs their own sending identity before they can scrape or send
    anything. This is the single enforcement point so individual routes
    don't each need their own check; see _ONBOARDING_EXEMPT_ENDPOINTS for
    what stays reachable regardless (account management, the connect flow
    itself, and anything a logged-out visitor can hit)."""
    if not require_login():
        return None
    endpoint = request.endpoint
    if endpoint in _ONBOARDING_EXEMPT_ENDPOINTS or (endpoint and endpoint.startswith("tracking.")):
        return None
    if db.get_google_account(session["username"]) or db.get_manual_email_account(session["username"]):
        return None
    if endpoint in _ONBOARDING_GATED_JSON_ENDPOINTS:
        return {"error": "Connect a Gmail account first."}, 403
    flash("Connect a Gmail account first — it's required before you can scrape or send.")
    return redirect(url_for("onboarding"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/users", methods=["GET", "POST"])
def users():
    """Visible to everyone logged in (so anyone can find the 'Leave access'
    option below) - but only the owner can remove *other* people, or see
    the sign-up link to invite them. New accounts are self-service (see
    signup()) - the owner no longer sets anyone else's password."""
    if not require_login():
        return redirect(url_for("login"))

    return render_template(
        "users.html",
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
    data_source.invalidate_user_backend_cache(session["username"])

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
    data_source.invalidate_user_backend_cache(session["username"])
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


@app.route("/connect/sheet", methods=["POST"])
def connect_sheet():
    """The no-OAuth-setup-needed way to get a personal Sheet: paste in a
    Sheet (shared as Editor with this app's service account) instead of
    going through Google Cloud Console OAuth, which only the owner can set
    up — see data_source.open_worksheet_by_id / sheets_source.service_account_email."""
    if not require_login():
        return redirect(url_for("login"))
    if not data_source.service_account_sheets_configured():
        flash("Connecting a Sheet isn't set up yet — ask whoever runs this app to add "
              "GOOGLE_SERVICE_ACCOUNT_FILE to .env.")
        return redirect(url_for("index"))

    form = ConnectSheetForm()
    if not form.validate_on_submit():
        flash("Enter a Google Sheet ID or URL.")
        return redirect(url_for("index"))

    sheet_id = _extract_sheet_id(form.sheet_id.data)
    worksheet_name = (form.worksheet_name.data or "").strip() or None

    try:
        ws = data_source.open_worksheet_by_id(sheet_id, worksheet_name)
        ws.row_values(1)  # forces a real API call now, so a bad id/permission shows up here, not on first use
    except gspread.exceptions.SpreadsheetNotFound:
        flash("Could not find that Sheet — double-check the ID or URL.")
        return redirect(url_for("index"))
    except gspread.exceptions.WorksheetNotFound:
        flash(f"That Sheet has no worksheet named '{worksheet_name}'.")
        return redirect(url_for("index"))
    except gspread.exceptions.APIError as e:
        if "must not be an Office file" in str(e):
            flash(
                "That's an uploaded Excel file (.xlsx), not a native Google Sheet — Google's "
                "Sheets API can only read/write real Sheets, even though Drive lets you preview "
                "an Excel file as one. Open it, then File → Save as Google Sheets to make a "
                "real copy, and connect that copy's URL instead (your original file is untouched)."
            )
            return redirect(url_for("index"))
        email = sheets_source.service_account_email()
        flash(f"Could not open that Sheet ({e}). Make sure it's shared as Editor with {email}.")
        return redirect(url_for("index"))
    except Exception as e:
        flash(f"Could not open that Sheet: {e}")
        return redirect(url_for("index"))

    db.save_user_sheet(session["username"], sheet_id, worksheet_name)
    data_source.invalidate_user_backend_cache(session["username"])

    try:
        backend = data_source.for_user(session["username"], require_owner())
        moved = data_source.migrate_to_sheet(session["username"], backend)
    except Exception:
        log.exception("Could not migrate local leads into the newly connected Sheet")
        moved = 0

    flash("Connected your Google Sheet." + (f" Moved {moved} existing lead(s) into it." if moved else ""))
    return redirect(url_for("index"))


@app.route("/connect/sheet/disconnect", methods=["POST"])
def connect_sheet_disconnect():
    if not require_login():
        return redirect(url_for("login"))
    db.delete_user_sheet(session["username"])
    data_source.invalidate_user_backend_cache(session["username"])
    flash("Disconnected your Google Sheet. New leads will go to your local file instead.")
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

        # Resolved up front (not just inside the send try/except below) so
        # create_recipient() can record WHICH mailbox each one is being sent
        # from — reply_checker.py needs that to know where to look for a
        # reply (see db.find_username_by_connected_email / db schema note).
        try:
            cfg, sender = sender_for_current_user()
        except Exception as e:
            flash(f"Could not resolve a sending identity: {e}")
            return render_template("index.html", form=form, results=None, scrape_status=scrape_job.get_status(), **_google_account_context())

        # --- create campaign + per-recipient tracking rows up front ---
        campaign_id = db.create_campaign(
            subject=form.subject.data,
            message=form.message.data,
            link_url=form.link_url.data,
            link_text=form.link_text.data,
            username=session["username"],
        )
        tokens = {}
        for email in to_send:
            _, token = db.create_recipient(campaign_id, email, sender_email=cfg.from_email)
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

    username = session["username"]
    overview = db.get_overview_stats(username)
    campaigns = db.get_campaigns_summary(username)
    timeseries = db.get_events_timeseries(username)
    sent_timeseries = db.get_sent_timeseries(username)
    recent_activity = db.get_recent_activity(username)

    sent = overview.get("total_sent") or 0
    reply_rate = round((overview.get("total_replies") or 0) / sent * 100) if sent else 0

    opens_by_day = [row for row in timeseries if row["event_type"] == "open"]
    sent_spark = _sparkline_points(sent_timeseries)
    needle_x, needle_y = _gauge_needle(reply_rate / 100)
    opens_spark = _sparkline_points(opens_by_day)

    try:
        backend = data_source.for_user(session["username"], require_owner())
    except Exception:
        log.exception("Could not resolve a leads backend for %s", username)
        backend = _UnavailableBackend()
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
        lead_journeys=db.get_lead_journeys(username),
        unseen_replies=db.get_unseen_reply_count(username),
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

    try:
        backend = data_source.for_user(session["username"], require_owner())
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
    try:
        backend = data_source.for_user(session["username"], require_owner())
        leads = _visible_leads(backend, session["username"])
    except Exception as e:
        return {"error": str(e)}, 500
    leads.sort(key=lambda lead: lead.get("row", 0), reverse=True)
    return {"leads": leads[:50], "backend_name": backend.name}


@app.route("/dashboard/replies-seen", methods=["POST"])
def mark_replies_seen():
    if not require_login():
        return redirect(url_for("login"))
    db.mark_all_replies_seen(session["username"])
    return redirect(url_for("dashboard"))


@app.route("/dashboard/journey/<int:recipient_id>/delete", methods=["POST"])
def delete_lead_journey(recipient_id):
    """Removes one row from the "Guest-post outreach" list — this app's
    own send/open/click/reply tracking, not the scraped-lead data itself
    (see /leads/clear for that)."""
    if not require_login():
        return redirect(url_for("login"))
    if db.delete_lead_journey(recipient_id, session["username"]):
        flash("Removed from the outreach list.")
    else:
        flash("Could not find that lead.")
    return redirect(url_for("dashboard"))


@app.route("/outreach/run", methods=["POST"])
def run_outreach():
    """Starts a background outreach-sending run and returns immediately -
    one email at a time with a throttled delay between each (see
    mailer._throttle_delay) easily takes minutes for a real batch, so the
    dashboard polls /outreach/status for live progress instead of this
    request blocking until every send finishes. See outreach_job.py."""
    if (refusal := _require_login_json()) is not None:
        return refusal
    try:
        cfg, sender = sender_for_current_user()
        backend = data_source.for_user(session["username"], require_owner())
    except Exception as e:
        return {"error": str(e)}, 500
    if not outreach_job.start(get_public_base_url(), cfg, sender, backend, session["username"]):
        return {"error": "An outreach run is already in progress."}, 409
    return outreach_job.get_status()


@app.route("/outreach/followups", methods=["POST"])
def run_followups():
    """Same background-job treatment as /outreach/run, for the Follow-ups button."""
    if (refusal := _require_login_json()) is not None:
        return refusal
    try:
        cfg, sender = sender_for_current_user()
    except Exception as e:
        return {"error": str(e)}, 500
    if not outreach_job.start_followups(get_public_base_url(), cfg, sender, session["username"]):
        return {"error": "A run is already in progress."}, 409
    return outreach_job.get_status()


@app.route("/outreach/status")
def outreach_status():
    if (refusal := _require_login_json()) is not None:
        return refusal
    return _status_for_viewer(outreach_job.get_status())


@app.route("/outreach/stop", methods=["POST"])
def outreach_stop():
    if (refusal := _require_login_json()) is not None:
        return refusal
    outreach_job.stop()
    return outreach_job.get_status()


# ---------------------------------------------------------------------------
# Lead scraping (JSON API, driven by fetch() from index.html — a real scrape
# runs for a while, so these never block a page load; the page polls
# /scrape/status instead).
# ---------------------------------------------------------------------------

def _require_login_json():
    """JSON equivalent of require_login() — redirecting an AJAX call to a
    login page would just leave the fetch() holding useless HTML."""
    return None if require_login() else ({"error": "login required"}, 401)


def _status_for_viewer(status: dict) -> dict:
    """scrape_job/outreach_job each track exactly one run at a time,
    process-wide — necessary since there's only one real Chrome window /
    one shared sent-count to update. But that means whatever the LAST run
    left behind (possibly another user's, possibly from minutes or days
    ago) would otherwise show up as "status" on the very next person's
    dashboard, regardless of who they are. Only the user who actually
    owns this status gets to see it; everyone else sees a plain idle
    state, same as if nothing had ever run."""
    if status.get("username") and status["username"] != session.get("username"):
        idle = {**status}
        idle["state"] = "idle"
        return idle
    return status


@app.route("/scrape/status")
def scrape_status():
    if (refusal := _require_login_json()) is not None:
        return refusal
    return _status_for_viewer(scrape_job.get_status())


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
        result = auto_outreach.send_pending_leads(get_public_base_url(), cfg, sender, backend, session["username"])
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
    # debug=False in anything resembling production.
    # threaded=True: the dashboard alone issues several DB queries and can
    # take a couple of seconds - without this, Flask's dev server handles
    # one request at a time, so a second user (or a reverse-proxy/tunnel's
    # own probe requests) queues up behind it and can time out.
    app.run(debug=True, port=5000, threaded=True)
