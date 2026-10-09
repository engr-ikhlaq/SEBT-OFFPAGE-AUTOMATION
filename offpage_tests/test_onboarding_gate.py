"""app.py's _require_gmail_connection before_request hook — Gmail is no
longer optional, so a logged-in user with neither a Google account nor a
manual one connected must be bounced to /onboarding from everywhere except
the exempt pages (account management, the connect flow itself, login/logout).
Uses Flask's test_client only - no live server, no network.

db.DB_PATH is pointed at a throwaway file BEFORE app.py is ever imported
(app.py runs db.init_db()/_seed_owner_account() at import time, and that
must never land on the real tracking.db) and again in setUp() for every
test (the Flask `app` object is a singleton - only created once no matter
how many test modules import it - so a module-level DB_PATH would get
silently overwritten by whichever test module happens to import app.py
last; setUp() re-pointing it per test is what actually isolates tests
from each other, regardless of import order)."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import db

db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "tracking.db"
db.init_db()
import app as app_module
import local_store

app_module.app.config["WTF_CSRF_ENABLED"] = False


def _client_logged_in_as(username: str):
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["username"] = username
        sess["role"] = "member"
    return client


class OnboardingGateTests(unittest.TestCase):
    def setUp(self):
        db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "tracking.db"
        db.init_db()
        db.create_user("alice", "hash", role="member", created_by="owner")
        db.create_user("connected", "hash", role="member", created_by="owner")
        db.save_manual_email_account("connected", "connected@gmail.com", "app-pass")

        # "connected" has no Sheet, so the dashboard falls through to the
        # local-file backend - user_leads_path() returns a RELATIVE path,
        # which would otherwise create a real leads_connected.xlsx in
        # whatever directory tests happen to run from.
        tmpdir = pathlib.Path(tempfile.mkdtemp())
        self._user_leads_path_patch = mock.patch.object(
            local_store, "user_leads_path", side_effect=lambda name: tmpdir / f"leads_{name}.xlsx"
        )
        self._user_leads_path_patch.start()
        self.addCleanup(self._user_leads_path_patch.stop)

    def test_an_unconnected_user_is_redirected_away_from_the_dashboard(self):
        client = _client_logged_in_as("alice")
        resp = client.get("/dashboard", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/onboarding", resp.headers["Location"])

    def test_an_unconnected_user_is_redirected_away_from_compose(self):
        client = _client_logged_in_as("alice")
        resp = client.get("/", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/onboarding", resp.headers["Location"])

    def test_a_connected_users_dashboard_loads_normally(self):
        client = _client_logged_in_as("connected")
        resp = client.get("/dashboard", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)

    def test_gated_json_endpoints_get_a_json_error_not_a_redirect(self):
        client = _client_logged_in_as("alice")
        resp = client.get("/scrape/status")
        self.assertEqual(resp.status_code, 403)
        self.assertIn("error", resp.get_json())

    def test_onboarding_itself_stays_reachable_while_unconnected(self):
        client = _client_logged_in_as("alice")
        resp = client.get("/onboarding")
        self.assertEqual(resp.status_code, 200)

    def test_users_page_stays_reachable_so_an_unconnected_user_can_still_leave(self):
        client = _client_logged_in_as("alice")
        resp = client.get("/users")
        self.assertEqual(resp.status_code, 200)

    def test_logged_out_visitor_is_not_touched_by_the_gate(self):
        client = app_module.app.test_client()
        resp = client.get("/dashboard", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])


if __name__ == "__main__":
    unittest.main()
