"""
link_paths.py
Single source of truth for the tracking/unsubscribe route paths, so the
Flask route decorators (tracking.py, app.py) and anything building links
into an email (app.py, auto_outreach.py) can't drift apart.
"""

OPEN_PIXEL_ROUTE = "/t/o/<token>.png"
CLICK_ROUTE = "/t/c/<token>"
UNSUBSCRIBE_ROUTE = "/unsubscribe"


def open_pixel_url(base_url: str, token: str) -> str:
    return f"{base_url}/t/o/{token}.png"


def click_url(base_url: str, token: str) -> str:
    return f"{base_url}/t/c/{token}"


def unsubscribe_url(base_url: str, email: str) -> str:
    return f"{base_url}{UNSUBSCRIBE_ROUTE}?email={email}"
