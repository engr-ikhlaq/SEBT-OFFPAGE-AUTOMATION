"""
tracking.py
Open tracking (1x1 pixel) and click tracking (redirect wrapper) endpoints.

How it works:
- Every recipient gets a unique random token (see db.create_recipient).
- The HTML email embeds an <img> pointing at /t/o/<token>.png. When a mail
  client loads images, that request logs an "open" event.
- The visible link in the email points at /t/c/<token> instead of the real
  URL. That route logs a "click" event, then 302-redirects to the real URL.

Honest limitation worth knowing: open tracking relies on the recipient's
mail client auto-loading remote images. Gmail/Outlook usually do by
default, but Apple Mail Privacy Protection and some privacy-focused
clients pre-fetch or block the pixel, which can produce false positives
or under-counts. Click tracking is far more reliable than open tracking
industry-wide, for this reason.
"""

from flask import Blueprint, Response, redirect, request, abort

import db

tracking_bp = Blueprint("tracking", __name__)

# 1x1 transparent GIF, served for every open-pixel request.
PIXEL_BYTES = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c00000000"
    "0100010000020144003b"
)


@tracking_bp.route("/t/o/<token>.png")
def track_open(token):
    db.record_open(
        token,
        user_agent=request.headers.get("User-Agent"),
        ip=request.headers.get("X-Forwarded-For", request.remote_addr),
    )
    resp = Response(PIXEL_BYTES, mimetype="image/gif")
    # Make sure mail clients / proxies don't cache the pixel and skip re-firing it.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@tracking_bp.route("/t/c/<token>")
def track_click(token):
    recipient = db.get_recipient_by_token(token)
    if not recipient:
        abort(404)

    campaign_id = recipient["campaign_id"]
    with db.get_conn() as conn:
        row = conn.execute("SELECT link_url FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    target = (row["link_url"] if row else None) or "/"

    db.record_click(
        token,
        user_agent=request.headers.get("User-Agent"),
        ip=request.headers.get("X-Forwarded-For", request.remote_addr),
    )
    return redirect(target, code=302)
