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
from mailer import MailerConfig, send_batch
from tracking import tracking_bp
import db
import sheets_source

load_dotenv()

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


def _any_imap_configured() -> bool:
    """True if either the main or the outreach mailbox has IMAP set up."""
    return bool(os.environ.get("IMAP_HOST") or os.environ.get("OUTREACH_IMAP_HOST"))


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
            return redirect(url_for("index"))
        flash("Incorrect username or password.")
    return render_template("login.html", form=form)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/users", methods=["GET", "POST"])
def users():
    if not require_login():
        return redirect(url_for("login"))
    if not require_owner():
        flash("Only the owner account can manage users.")
        return redirect(url_for("dashboard"))

    form = AddUserForm()
    if form.validate_on_submit():
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

    return render_template("users.html", form=form, users=db.list_users(), current_user_id=session["user_id"])


@app.route("/users/<int:user_id>/delete", methods=["POST"])
def delete_user(user_id):
    if not require_login():
        return redirect(url_for("login"))
    if not require_owner():
        flash("Only the owner account can manage users.")
        return redirect(url_for("dashboard"))

    if user_id == session["user_id"]:
        flash("You can't remove your own access.")
    elif not db.delete_user(user_id):
        flash("Can't remove the last remaining account.")
    else:
        flash("Access removed.")
    return redirect(url_for("users"))


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
                return render_template("index.html", form=form, results=None)
            try:
                sheet_recipients = sheets_source.read_recipients(skip_already_sent=True)
            except Exception as e:
                flash(f"Could not read Google Sheet: {e}")
                return render_template("index.html", form=form, results=None)

            for rec in sheet_recipients:
                email = rec["Email"].strip()
                raw_lines.append(email)
                sheet_row_by_email[email.strip().lower()] = rec["row"]

        if not raw_lines:
            flash("Provide recipients by pasting them, uploading a file, or enabling the Google Sheet source.")
            return render_template("index.html", form=form, results=None)

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
            cfg = get_mailer_config()
            results = send_batch(
                cfg,
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

    return render_template("index.html", form=form, results=results)


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
    opens_spark = _sparkline_points(opens_by_day)

    return render_template(
        "dashboard.html",
        overview=overview,
        campaigns=campaigns,
        sent_spark=sent_spark,
        opens_spark=opens_spark,
        timeseries=timeseries,
        sent_timeseries=sent_timeseries,
        reply_rate=reply_rate,
        recent_activity=recent_activity,
        imap_configured=_any_imap_configured(),
        lead_journeys=db.get_lead_journeys(),
        unseen_replies=db.get_unseen_reply_count(),
        sheet_configured=sheets_source.is_configured(),
        is_owner=require_owner(),
    )


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

    if not sheets_source.is_configured():
        flash("Google Sheet isn't configured — set GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_SHEET_ID in .env.")
        return redirect(url_for("dashboard"))

    try:
        result = auto_outreach.send_pending_leads(get_public_base_url())
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
        result = auto_outreach.send_due_followups(get_public_base_url())
        flash(f"Follow-ups complete — sent {len(result['sent'])}, failed {len(result['failed'])}.")
    except Exception as e:
        flash(f"Follow-up run failed: {e}")

    return redirect(url_for("dashboard"))


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
