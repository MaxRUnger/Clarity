"""Idle timeout for the professor session cookie."""
import os
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


class TestSessionTimeout(unittest.TestCase):
    def setUp(self):
        self.app = _app()
        self.client = self.app.test_client()

    def _sign_in_at(self, timestamp):
        with patch("itsdangerous.timed.time.time", return_value=timestamp):
            with self.client.session_transaction() as sess:
                sess["user_id"] = "prof-1"
                sess["role"] = "instructor"
                sess["csrf_token"] = "tok"

    def test_status_reports_time_left_and_does_not_slide(self):
        t0 = 1_700_000_000
        self._sign_in_at(t0)
        with patch("itsdangerous.timed.time.time", return_value=t0 + 100 * 60), patch(
            "app.routes.time.time", return_value=t0 + 100 * 60
        ):
            status = self.client.get("/api/session-status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.headers.get("Cache-Control"), "no-store")
        body = status.get_json()
        self.assertTrue(body["ok"])
        self.assertLessEqual(body["seconds_left"], 7200)
        self.assertGreater(body["seconds_left"], 0)
        session_cookies = [
            cookie for cookie in status.headers.getlist("Set-Cookie")
            if cookie.lower().startswith("clarity_session=")
        ]
        self.assertEqual(session_cookies, [])
        with patch("itsdangerous.timed.time.time", return_value=t0 + 200 * 60):
            probe = self.client.get("/_session_probe")
        self.assertIsNone(probe.get_json()["user_id"])

    def test_keepalive_slides_and_requires_csrf(self):
        t0 = 1_700_000_000
        self._sign_in_at(t0)
        with patch("itsdangerous.timed.time.time", return_value=t0 + 60):
            missing = self.client.post("/api/session-keepalive")
        self.assertEqual(missing.status_code, 403)
        with patch("itsdangerous.timed.time.time", return_value=t0 + 100 * 60):
            kept = self.client.post(
                "/api/session-keepalive",
                headers={"X-CSRF-Token": "tok"},
            )
        self.assertEqual(kept.status_code, 200)
        with patch("itsdangerous.timed.time.time", return_value=t0 + 200 * 60):
            probe = self.client.get("/_session_probe")
        self.assertEqual(probe.get_json()["user_id"], "prof-1")

    def test_expired_keepalive_and_save_return_session_expired(self):
        t0 = 1_700_000_000
        self._sign_in_at(t0)
        with patch("itsdangerous.timed.time.time", return_value=t0 + 121 * 60):
            kept = self.client.post(
                "/api/session-keepalive",
                headers={"X-CSRF-Token": "tok"},
            )
            saved = self.client.post(
                "/api/class/c1/save-grades",
                json={"grades": {}, "assignment_id": "a1"},
                headers={"X-CSRF-Token": "tok"},
            )
        for response in (kept, saved):
            self.assertEqual(response.status_code, 401)
            self.assertNotEqual(response.status_code, 403)
            self.assertNotIn("Location", response.headers)
            self.assertEqual(response.get_json()["code"], "session_expired")
            self.assertEqual(response.get_json()["error"], "Unauthorized")

    def test_three_minute_lifetime_scales_warning_and_keepalive(self):
        from app import session_timeout_settings
        app = create_app()
        app.permanent_session_lifetime = timedelta(minutes=3)
        settings = session_timeout_settings(app)
        self.assertEqual(settings["idle_seconds"], 180)
        self.assertEqual(settings["warning_seconds"], 60)
        self.assertEqual(settings["keepalive_seconds"], 45)

    def test_login_inactivity_message_is_exact(self):
        shown = self.client.get("/login", query_string={"reason": "inactivity"})
        self.assertIn("You were signed out due to inactivity.", shown.get_data(as_text=True))
        self.assertIn('id="inactivityNotice"', shown.get_data(as_text=True))
        payload = "<script>alert(1)</script>"
        hidden = self.client.get("/login", query_string={"reason": payload})
        body = hidden.get_data(as_text=True)
        self.assertNotIn("inactivityNotice", body)
        self.assertNotIn("You were signed out due to inactivity.", body)
        self.assertNotIn(payload, body)
        self.assertNotIn("alert(1)", body)
        self.assertNotIn("&lt;script&gt;", body)

    def test_session_script_source_and_placement(self):
        root = os.path.dirname(__file__)
        script_path = os.path.join(root, "..", "app", "static", "js", "session_timeout.js")
        with open(script_path, encoding="utf-8") as fh:
            script = fh.read()
        for token in (
            "clarity.sessionActivity",
            "clarity.sessionKeepalive",
            "activity > lastSent",
            "Date.now()",
            "visibilitychange",
            "focus",
            "online",
            "pageshow",
            "event.persisted",
            "response.clone()",
            "alertdialog",
            "print:hidden",
            "Stay signed in",
            "redirectToLogout(true)",
            "redirectToLogout(false)",
            "redirecting",
            "window.runAutosave",
            "Promise.race",
            "5000",
            "/logout?reason=inactivity",
            "sessionTimeoutConfig",
            "/api/session-status",
            "/api/session-keepalive",
            "memoryStamps",
            "status >= 500",
            "statusCheckInFlight",
            "10000",
            "writeStamp(ACTIVITY_KEY, started)",
            "writeStamp(KEEPALIVE_KEY, started)",
            "Your session expired. Signing out.",
            'classList.add("hidden")',
            'classList.remove("hidden")',
            "hidden fixed inset-0",
            "lastKeepAliveAttempt",
            "Math.min(15000, keepaliveMs)",
        ):
            self.assertIn(token, script)
        self.assertNotIn("if (!readStamp(ACTIVITY_KEY))", script)
        self.assertNotIn("if (!readStamp(KEEPALIVE_KEY))", script)
        self.assertNotIn("if (!writeStamp(KEEPALIVE_KEY, now))", script)
        self.assertNotIn("7200", script)
        self.assertNotIn("beforeunload", script)
        self.assertNotIn(".hidden =", script)
        for name in ("app_base.html", "auth_base.html"):
            page_path = os.path.join(root, "..", "app", "templates", name)
            with open(page_path, encoding="utf-8") as fh:
                page = fh.read()
            start = page.find("{% if session.get('user_id') %}")
            end = page.find("{% endif %}", start)
            self.assertIn("session_timeout.js", page[start:end])
        for name in ("login.html", "signup.html"):
            page_path = os.path.join(root, "..", "app", "templates", name)
            with open(page_path, encoding="utf-8") as fh:
                page = fh.read()
            self.assertNotIn("session_timeout.js", page)

    def test_expired_fetch_stays_pending_after_logout_starts(self):
        root = os.path.dirname(__file__)
        script_path = os.path.join(root, "..", "app", "static", "js", "session_timeout.js")
        with open(script_path, encoding="utf-8") as fh:
            script = fh.read()
        self.assertIn("logoutAutosaveRunning", script)
        wrapper_at = script.find("window.fetch = function")
        self.assertGreaterEqual(wrapper_at, 0)
        wrapper = script[wrapper_at:]
        redirect_at = wrapper.find("if (redirecting)")
        promise_at = wrapper.find("new Promise(function () {})")
        self.assertGreaterEqual(redirect_at, 0)
        self.assertGreater(promise_at, redirect_at)
        self.assertIn("function settleExpiredFetch(response)", wrapper)
        self.assertEqual(wrapper.count("return settleExpiredFetch(response)"), 2)
