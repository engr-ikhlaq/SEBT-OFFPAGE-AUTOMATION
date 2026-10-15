"""app.py's _require_sender_name before_request hook - a connected
mailbox with no sender_name set yet must be bounced to
/account/sender-name from everywhere except the exempt pages, so an
outreach email never goes out signed with the wrong name (see the real
bug this fixes: a manually-connected mailbox signing off with the LOGIN
account's name instead of its own). Uses Flask's test_client only."""
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


class SenderNameGateTests(unittest.TestCase):
    def setUp(self):
        db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "tracking.db"
        db.init_db()
        db.create_user("unnamed", "hash", role="member", created_by="owner")
        db.create_user("named", "hash", role="member", created_by="owner")
        db.save_manual_email_account("unnamed", "unnamed@gmail.com", "app-pass")  # no sender_name
        db.save_manual_email_account("named", "named@gmail.com", "app-pass", sender_name="Maira")

        tmpdir = pathlib.Path(tempfile.mkdtemp())
        self._user_leads_path_patch = mock.patch.object(
            local_store, "user_leads_path", side_effect=lambda name: tmpdir / f"leads_{name}.xlsx"
        )
        self._user_leads_path_patch.start()
        self.addCleanup(self._user_leads_path_patch.stop)

    def test_a_connected_but_unnamed_mailbox_is_redirected_to_set_a_name(self):
        client = _client_logged_in_as("unnamed")
        resp = client.get("/dashboard", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/account/sender-name", resp.headers["Location"])

    def test_a_named_mailbox_loads_the_dashboard_normally(self):
        client = _client_logged_in_as("named")
        resp = client.get("/dashboard", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)

    def test_gated_json_endpoints_get_a_json_error_not_a_redirect(self):
        client = _client_logged_in_as("unnamed")
        resp = client.get("/scrape/status")
        self.assertEqual(resp.status_code, 403)
        self.assertIn("error", resp.get_json())

    def test_the_sender_name_page_itself_stays_reachable(self):
        client = _client_logged_in_as("unnamed")
        resp = client.get("/account/sender-name")
        self.assertEqual(resp.status_code, 200)

    def test_saving_a_name_clears_the_redirect(self):
        client = _client_logged_in_as("unnamed")
        resp = client.post("/account/sender-name", data={"sender_name": "Maira"}, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/", resp.headers["Location"])

        account = db.get_manual_email_account("unnamed")
        self.assertEqual(account["sender_name"], "Maira")

        resp = client.get("/dashboard", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)  # no longer bounced


if __name__ == "__main__":
    unittest.main()
