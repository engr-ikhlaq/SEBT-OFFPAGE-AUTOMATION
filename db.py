"""
db.py
Lightweight SQLite storage for campaign + recipient tracking.

No ORM on purpose — this is a small personal tool, plain sqlite3 keeps it
easy to inspect (just open tracking.db in any SQLite browser) and easy to
back up (it's a single file).
"""

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "tracking.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subject         TEXT NOT NULL,
    message         TEXT NOT NULL,
    link_url        TEXT,
    link_text       TEXT,
    created_at      TEXT NOT NULL,
    sent_count      INTEGER NOT NULL DEFAULT 0,
    failed_count    INTEGER NOT NULL DEFAULT 0,
    invalid_count   INTEGER NOT NULL DEFAULT 0,
    skipped_count   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS recipients (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id       INTEGER NOT NULL REFERENCES campaigns(id),
    email             TEXT NOT NULL,
    token             TEXT NOT NULL UNIQUE,
    status            TEXT NOT NULL DEFAULT 'pending',  -- pending/sent/failed
    error             TEXT,
    message_id        TEXT,
    sent_at           TEXT,
    open_count        INTEGER NOT NULL DEFAULT 0,
    first_opened_at   TEXT,
    last_opened_at    TEXT,
    click_count       INTEGER NOT NULL DEFAULT 0,
    first_clicked_at  TEXT,
    last_clicked_at   TEXT,
    replied_at        TEXT,
    unsubscribed_at   TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_id  INTEGER NOT NULL REFERENCES recipients(id),
    event_type    TEXT NOT NULL,   -- open / click / reply
    occurred_at   TEXT NOT NULL,
    user_agent    TEXT,
    ip            TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'member',  -- 'owner' can manage other users
    created_at    TEXT NOT NULL,
    created_by    TEXT
);

-- One connected Google account per app user (see google_oauth.py). No hard
-- foreign key to users(username) on purpose - delete_user() removes the
-- matching row itself (see below), and a loose reference here means that
-- stays a simple two-statement delete instead of needing ON DELETE CASCADE
-- or risking an IntegrityError if the cleanup is ever missed.
-- The refresh token is the sensitive part - it doesn't expire until the
-- user revokes access at myaccount.google.com/permissions, so this file is
-- effectively as sensitive as a password and must stay out of git (see
-- .gitignore: tracking.db already is).
CREATE TABLE IF NOT EXISTS google_accounts (
    username        TEXT PRIMARY KEY,
    google_email    TEXT NOT NULL,
    refresh_token   TEXT NOT NULL,
    access_token    TEXT,
    sheet_id        TEXT,
    connected_at    TEXT NOT NULL
);

-- The no-OAuth-setup-needed alternative: a Gmail address + app password,
-- typed directly into the app (see app.py's /connect/gmail-manual). Works
-- immediately, with none of the admin-side Cloud Console setup OAuth
-- needs. If a user has BOTH this and an OAuth connection, OAuth wins
-- (sender_for_current_user() checks it first) - this is the fallback
-- for whoever hasn't done, or doesn't want to do, OAuth.
CREATE TABLE IF NOT EXISTS manual_email_accounts (
    username        TEXT PRIMARY KEY,
    email           TEXT NOT NULL,
    app_password    TEXT NOT NULL,
    connected_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recipients_campaign ON recipients(campaign_id);
CREATE INDEX IF NOT EXISTS idx_recipients_token ON recipients(token);
CREATE INDEX IF NOT EXISTS idx_events_recipient ON events(recipient_id);
"""

# Columns added after the original schema. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so each is tried and a "duplicate column" failure is ignored —
# the normal way to keep an existing tracking.db file working after an
# upgrade, without a separate migration-numbering system for a single-file
# personal tool.
_COLUMN_MIGRATIONS = (
    "ALTER TABLE recipients ADD COLUMN kind TEXT NOT NULL DEFAULT 'campaign'",  # 'campaign' or 'outreach'
    "ALTER TABLE recipients ADD COLUMN sheet_row INTEGER",
    "ALTER TABLE recipients ADD COLUMN lead_domain TEXT",
    "ALTER TABLE recipients ADD COLUMN lead_url TEXT",
    "ALTER TABLE recipients ADD COLUMN followed_up_at TEXT",
    "ALTER TABLE recipients ADD COLUMN reply_seen_at TEXT",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for statement in _COLUMN_MIGRATIONS:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise


def create_campaign(subject, message, link_url, link_text) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO campaigns (subject, message, link_url, link_text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (subject, message, link_url, link_text, now_iso()),
        )
        return cur.lastrowid


def create_recipient(campaign_id: int, email: str, kind: str = "campaign",
                      sheet_row: int = None, lead_domain: str = None,
                      lead_url: str = None) -> tuple[int, str]:
    """Creates a pending recipient row with a fresh tracking token. Returns (id, token).

    kind/sheet_row/lead_domain/lead_url are set for leads sourced from the
    connected sheet (see auto_outreach.py), so a follow-up or the dashboard
    can show which lead and which sheet row a recipient came from.
    """
    token = uuid.uuid4().hex
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO recipients (campaign_id, email, token, status, kind, sheet_row, lead_domain, lead_url) "
            "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)",
            (campaign_id, email, token, kind, sheet_row, lead_domain, lead_url),
        )
        return cur.lastrowid, token


def mark_recipient_result(email: str, campaign_id: int, status: str,
                           error: str = None, message_id: str = None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE recipients SET status=?, error=?, message_id=?, sent_at=? "
            "WHERE campaign_id=? AND email=?",
            (status, error, message_id, now_iso() if status == "sent" else None,
             campaign_id, email),
        )


def finalize_campaign_counts(campaign_id: int, sent: int, failed: int,
                              invalid: int, skipped: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE campaigns SET sent_count=?, failed_count=?, invalid_count=?, skipped_count=? "
            "WHERE id=?",
            (sent, failed, invalid, skipped, campaign_id),
        )


def get_recipient_by_token(token: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM recipients WHERE token=?", (token,)).fetchone()
        return dict(row) if row else None


def record_open(token: str, user_agent: str = None, ip: str = None):
    with get_conn() as conn:
        row = conn.execute("SELECT id, open_count, first_opened_at FROM recipients WHERE token=?",
                            (token,)).fetchone()
        if not row:
            return False
        ts = now_iso()
        conn.execute(
            "UPDATE recipients SET open_count = open_count + 1, "
            "first_opened_at = COALESCE(first_opened_at, ?), last_opened_at = ? "
            "WHERE id = ?",
            (ts, ts, row["id"]),
        )
        conn.execute(
            "INSERT INTO events (recipient_id, event_type, occurred_at, user_agent, ip) "
            "VALUES (?, 'open', ?, ?, ?)",
            (row["id"], ts, user_agent, ip),
        )
        return True


def record_click(token: str, user_agent: str = None, ip: str = None):
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM recipients WHERE token=?", (token,)).fetchone()
        if not row:
            return False
        ts = now_iso()
        conn.execute(
            "UPDATE recipients SET click_count = click_count + 1, "
            "first_clicked_at = COALESCE(first_clicked_at, ?), last_clicked_at = ? "
            "WHERE id = ?",
            (ts, ts, row["id"]),
        )
        conn.execute(
            "INSERT INTO events (recipient_id, event_type, occurred_at, user_agent, ip) "
            "VALUES (?, 'click', ?, ?, ?)",
            (row["id"], ts, user_agent, ip),
        )
        return True


def record_reply(recipient_id: int):
    with get_conn() as conn:
        ts = now_iso()
        conn.execute(
            "UPDATE recipients SET replied_at = COALESCE(replied_at, ?) WHERE id = ?",
            (ts, recipient_id),
        )
        conn.execute(
            "INSERT INTO events (recipient_id, event_type, occurred_at) VALUES (?, 'reply', ?)",
            (recipient_id, ts),
        )


def mark_unsubscribed(email: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE recipients SET unsubscribed_at=? WHERE email=? AND unsubscribed_at IS NULL",
            (now_iso(), email),
        )


# ---------------------------------------------------------------------------
# Dashboard aggregate queries
# ---------------------------------------------------------------------------

def get_overview_stats():
    with get_conn() as conn:
        totals = conn.execute("""
            SELECT
                COUNT(*)                                    AS total_recipients,
                SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END)          AS total_sent,
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END)        AS total_failed,
                SUM(CASE WHEN open_count > 0 THEN 1 ELSE 0 END)         AS unique_opens,
                SUM(CASE WHEN click_count > 0 THEN 1 ELSE 0 END)        AS unique_clicks,
                SUM(CASE WHEN replied_at IS NOT NULL THEN 1 ELSE 0 END) AS total_replies,
                SUM(CASE WHEN unsubscribed_at IS NOT NULL THEN 1 ELSE 0 END) AS total_unsubs,
                SUM(open_count)  AS total_open_events,
                SUM(click_count) AS total_click_events
            FROM recipients
        """).fetchone()
        campaign_count = conn.execute("SELECT COUNT(*) c FROM campaigns").fetchone()["c"]
        return {**dict(totals), "campaign_count": campaign_count}


def get_campaigns_summary():
    """Manually-composed campaigns only — auto-outreach leads get their own
    per-lead journey view (get_lead_journeys) instead of cluttering this
    table with one row per lead."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                c.id, c.subject, c.created_at, c.sent_count, c.failed_count,
                c.invalid_count, c.skipped_count,
                SUM(CASE WHEN r.open_count > 0 THEN 1 ELSE 0 END)  AS opens,
                SUM(CASE WHEN r.click_count > 0 THEN 1 ELSE 0 END) AS clicks,
                SUM(CASE WHEN r.replied_at IS NOT NULL THEN 1 ELSE 0 END) AS replies
            FROM campaigns c
            LEFT JOIN recipients r ON r.campaign_id = c.id
            WHERE c.id NOT IN (SELECT DISTINCT campaign_id FROM recipients WHERE kind='outreach')
            GROUP BY c.id
            ORDER BY c.id DESC
        """).fetchall()
        return [dict(r) for r in rows]


def get_campaign_detail(campaign_id: int):
    with get_conn() as conn:
        campaign = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not campaign:
            return None
        recipients = conn.execute(
            "SELECT * FROM recipients WHERE campaign_id=? ORDER BY id", (campaign_id,)
        ).fetchall()
        return {"campaign": dict(campaign), "recipients": [dict(r) for r in recipients]}


def get_sent_timeseries(days: int = 7):
    """Sends per day for the last N days, oldest first — for the dashboard sparklines."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT substr(sent_at, 1, 10) AS day, COUNT(*) AS n
            FROM recipients
            WHERE status='sent' AND sent_at IS NOT NULL
            GROUP BY day
            ORDER BY day DESC
            LIMIT ?
        """, (days,)).fetchall()
        return list(reversed([dict(r) for r in rows]))


def get_events_timeseries(days: int = 14):
    """Opens + clicks per day for the last N days, for the trend chart."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT substr(occurred_at, 1, 10) AS day, event_type, COUNT(*) AS n
            FROM events
            WHERE event_type IN ('open', 'click')
            GROUP BY day, event_type
            ORDER BY day
        """).fetchall()
        return [dict(r) for r in rows]


def get_recent_activity(limit: int = 25):
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT e.event_type, e.occurred_at, r.email, c.subject, c.id AS campaign_id
            FROM events e
            JOIN recipients r ON r.id = e.recipient_id
            JOIN campaigns c ON c.id = r.campaign_id
            ORDER BY e.occurred_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_all_recipients_for_reply_check(only_kind: str = None, exclude_kind: str = None):
    """Recipients that were successfully sent to and don't have a reply logged yet.

    only_kind/exclude_kind let a caller split this by 'kind' (e.g. checking
    outreach leads against a different mailbox than everything else) —
    see reply_checker.py.
    """
    query = """
        SELECT id, campaign_id, email, message_id, sent_at
        FROM recipients
        WHERE status='sent' AND message_id IS NOT NULL AND replied_at IS NULL
    """
    params: list[str] = []
    if only_kind:
        query += " AND kind=?"
        params.append(only_kind)
    if exclude_kind:
        query += " AND kind!=?"
        params.append(exclude_kind)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Outreach leads + follow-ups
# ---------------------------------------------------------------------------

def get_recipients_due_for_followup(hours: int = 24):
    """Outreach leads sent more than `hours` ago, with no reply and no follow-up yet."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, campaign_id, email, message_id, sent_at, lead_domain, lead_url
            FROM recipients
            WHERE kind='outreach' AND status='sent' AND message_id IS NOT NULL
              AND replied_at IS NULL AND followed_up_at IS NULL
              AND unsubscribed_at IS NULL
              AND sent_at <= ?
        """, (cutoff,)).fetchall()
        return [dict(r) for r in rows]


def mark_followed_up(recipient_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE recipients SET followed_up_at=? WHERE id=?",
            (now_iso(), recipient_id),
        )


def get_lead_journeys(limit: int = 100):
    """Outreach leads with their current stage, newest first — for the dashboard timeline."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, email, lead_domain, status, error, sent_at, open_count,
                   first_opened_at, click_count, first_clicked_at, replied_at,
                   followed_up_at, unsubscribed_at
            FROM recipients
            WHERE kind='outreach'
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_unseen_reply_count() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM recipients WHERE replied_at IS NOT NULL AND reply_seen_at IS NULL"
        ).fetchone()
        return row["c"]


def mark_all_replies_seen():
    with get_conn() as conn:
        conn.execute(
            "UPDATE recipients SET reply_seen_at=? WHERE replied_at IS NOT NULL AND reply_seen_at IS NULL",
            (now_iso(),),
        )


# ---------------------------------------------------------------------------
# Users (multi-user login)
# ---------------------------------------------------------------------------

def create_user(username: str, password_hash: str, role: str = "member",
                 created_by: str = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, password_hash, role, now_iso(), created_by),
        )
        return cur.lastrowid


def get_user_by_username(username: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None


def list_users():
    with get_conn() as conn:
        rows = conn.execute("SELECT id, username, role, created_at, created_by FROM users ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def count_users() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]


def delete_user(user_id: int) -> bool:
    """Refuses to delete the last remaining user, so the app can't lock everyone out."""
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        if total <= 1:
            return False
        row = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        if row:
            conn.execute("DELETE FROM google_accounts WHERE username=?", (row["username"],))
            conn.execute("DELETE FROM manual_email_accounts WHERE username=?", (row["username"],))
        return True


# ---------------------------------------------------------------------------
# Connected Google accounts (one per app user — see google_oauth.py)
# ---------------------------------------------------------------------------

def save_google_account(username: str, google_email: str, refresh_token: str,
                         access_token: str, sheet_id: str | None) -> None:
    """Upsert: connecting again (e.g. after revoking) just replaces the row."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO google_accounts (username, google_email, refresh_token, access_token, sheet_id, connected_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(username) DO UPDATE SET "
            "  google_email=excluded.google_email, refresh_token=excluded.refresh_token, "
            "  access_token=excluded.access_token, sheet_id=excluded.sheet_id, connected_at=excluded.connected_at",
            (username, google_email, refresh_token, access_token, sheet_id, now_iso()),
        )


def get_google_account(username: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM google_accounts WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None


def update_google_access_token(username: str, access_token: str) -> None:
    """Called after a refresh, so the next send can reuse the fresh token
    instead of refreshing again immediately."""
    with get_conn() as conn:
        conn.execute("UPDATE google_accounts SET access_token=? WHERE username=?", (access_token, username))


def delete_google_account(username: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM google_accounts WHERE username=?", (username,))


# ---------------------------------------------------------------------------
# Manually-connected Gmail (email + app password, no OAuth setup needed)
# ---------------------------------------------------------------------------

def save_manual_email_account(username: str, email: str, app_password: str) -> None:
    """Upsert: connecting again just replaces the stored password."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO manual_email_accounts (username, email, app_password, connected_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(username) DO UPDATE SET "
            "  email=excluded.email, app_password=excluded.app_password, connected_at=excluded.connected_at",
            (username, email, app_password, now_iso()),
        )


def get_manual_email_account(username: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM manual_email_accounts WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None


def delete_manual_email_account(username: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM manual_email_accounts WHERE username=?", (username,))
