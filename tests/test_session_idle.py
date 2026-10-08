"""Idle timeout for the professor session cookie."""
import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch

from flask import jsonify, session

from app import create_app


def _app():
    app = create_app()
    app.config["TESTING"] = True

    @app.route("/_session_probe")
    def _session_probe():
        return jsonify({
            "user_id": session.get("user_id"),
            "permanent": bool(session.permanent),
        })

    return app


class TestIdleSession(unittest.TestCase):
    def setUp(self):
        self.app = _app()
        self.assertEqual(self.app.permanent_session_lifetime, timedelta(minutes=120))
        self.client = self.app.test_client()

    def _sign_in_at(self, timestamp):
        with patch("itsdangerous.timed.time.time", return_value=timestamp):
            with self.client.session_transaction() as sess:
                sess["user_id"] = "prof-1"
                sess["role"] = "instructor"
                sess["csrf_token"] = "tok"

    def test_idle_session_expires_after_121_minutes(self):
        t0 = 1_700_000_000
        self._sign_in_at(t0)
        with patch("itsdangerous.timed.time.time", return_value=t0 + 121 * 60):
            rv = self.client.get("/_session_probe")
        self.assertIsNone(rv.get_json()["user_id"])

    def test_activity_across_two_100_minute_gaps_keeps_the_session(self):
        t0 = 1_700_000_000
        self._sign_in_at(t0)
        with patch("itsdangerous.timed.time.time", return_value=t0 + 100 * 60):
            first = self.client.get("/_session_probe")
        self.assertEqual(first.get_json()["user_id"], "prof-1")
        self.assertFalse(first.get_json()["permanent"])
        with patch("itsdangerous.timed.time.time", return_value=t0 + 200 * 60):
            second = self.client.get("/_session_probe")
        self.assertEqual(second.get_json()["user_id"], "prof-1")

    def test_login_cookie_has_no_expires_or_max_age(self):
        user = MagicMock()
        user.id = "user-1"
        user.user_metadata = {"role": "instructor", "full_name": "Ada"}
        result = MagicMock()
        result.user = user
        profile = MagicMock()
        profile.data = [{"role": "instructor"}]
        with patch("app.routes.supabase") as sb, \
             patch("app.routes.supabase_admin") as admin, \
             patch("app.routes.ensure_profile_exists"):
            sb.auth.sign_in_with_password.return_value = result
            (
                admin.table.return_value
                .select.return_value
                .eq.return_value
                .limit.return_value
                .execute.return_value
            ) = profile
            rv = self.client.post(
                "/api/login",
                json={"email": "ada@school.edu", "password": "secret"},
            )
        self.assertEqual(rv.status_code, 200)
        session_cookies = [
            cookie for cookie in rv.headers.getlist("Set-Cookie")
            if cookie.startswith("clarity_session=")
        ]
        self.assertEqual(len(session_cookies), 1)
        header = session_cookies[0].lower()
        self.assertNotIn("expires=", header)
        self.assertNotIn("max-age=", header)

    def test_logout_deletes_the_cookie_and_the_next_request_has_no_user(self):
        self._sign_in_at(1_700_000_000)
        rv = self.client.get("/logout")
        self.assertEqual(rv.status_code, 302)
        deleted = [
            cookie for cookie in rv.headers.getlist("Set-Cookie")
            if cookie.lower().startswith("clarity_session=")
        ]
        self.assertEqual(len(deleted), 1)
        self.assertIn("max-age=0", deleted[0].lower())
        nxt = self.client.get("/_session_probe")
        self.assertIsNone(nxt.get_json()["user_id"])

    def test_static_request_does_not_set_a_cookie(self):
        self._sign_in_at(1_700_000_000)
        rv = self.client.get("/static/js/ui_helpers.js")
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.headers.getlist("Set-Cookie"), [])
