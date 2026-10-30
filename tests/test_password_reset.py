"""Password reset and the shared password rules."""
import logging
import os
import re
import time
import unittest
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import quote

from app import create_app
from app.app_sessions import (
    cached_app_session,
    clear_app_session_cache,
    remember_app_session,
)

FORGOT_OK = {
    "success": True,
    "message": "If an instructor account exists for that address, a reset message is on its way.",
}
RATE_LIMIT = {
    "success": False,
    "error": "Rate limit exceeded. Please retry shortly.",
}
MSG_SHORT = "Use at least 12 characters."
MSG_LONG = "Use at most 72 characters."
MSG_LOWER = "Use at least one lowercase letter."
MSG_UPPER = "Use at least one uppercase letter."
MSG_DIGIT = "Use at least one digit."
MSG_SYMBOL = "Use at least one symbol such as ! or #."
MSG_EMAIL = "Do not use your email address as the password."
MSG_LOCAL = "Do not include the start of your email address."
MSG_WORD = "Do not use a common word as the password."
MSG_SAVE = "We couldn't save the new password. Request a new reset link."
REQUIRED_WORDS = {
    "password",
    "passw0rd",
    "welcome",
    "letmein",
    "qwerty",
    "qwertyuiop",
    "admin",
    "administrator",
    "iloveyou",
    "changeme",
    "clarity",
    "claritygrader",
    "palomar",
    "instructor",
    "professor",
    "teacher",
}
PASSWORD_CASES = [
    ("Aa1!bbbbbbb", "ada.lovelace@school.edu", MSG_SHORT),
    ("Aa1!bbbbbbbb", "ada.lovelace@school.edu", None),
    ("Aa1!" + ("b" * 68), "ada.lovelace@school.edu", None),
    ("Aa1!" + ("b" * 69), "ada.lovelace@school.edu", MSG_LONG),
    ("Aa1!" + ("\u00e9" * 35), "ada.lovelace@school.edu", MSG_LONG),
    ("Aa\u00b2!bbbbbbbb", "ada.lovelace@school.edu", MSG_DIGIT),
    ("AA1!BBBBBBBB", "ada.lovelace@school.edu", MSG_LOWER),
    ("aa1!bbbbbbbb", "ada.lovelace@school.edu", MSG_UPPER),
    ("Aa!!bbbbbbbb", "ada.lovelace@school.edu", MSG_DIGIT),
    ("Aa12bbbbbbbb", "ada.lovelace@school.edu", MSG_SYMBOL),
    ("Aa1 bbbbbbbb", "ada.lovelace@school.edu", MSG_SYMBOL),
    ("Aa1!bbb bbbb", "ada.lovelace@school.edu", None),
    ("Password123!", "ada.lovelace@school.edu", MSG_WORD),
    ("  User1!@Ab.co  ", "User1!@ab.co", MSG_EMAIL),
    ("Xada.LOVELACE1!", "ada.lovelace@school.edu", MSG_LOCAL),
    ("Xxab99!aaaaa", "ab@school.edu", None),
    ("UniqueZebra99", "ada.lovelace@school.edu", MSG_SYMBOL),
]


def _load_passwords():
    try:
        from app import passwords
    except ImportError as exc:
        raise AssertionError("shared password validator is missing") from exc
    return passwords


def _resend_ok():
    response = MagicMock(status_code=200)
    response.json.return_value = {"id": "msg_1"}
    return response


def _link(user_id="instructor-1", email="ada.lovelace@school.edu"):
    result = MagicMock()
    result.user.id = user_id
    result.user.email = email
    result.properties.hashed_token = "hashedtoken"
    result.properties.action_link = (
        "https://abc.supabase.co/auth/v1/verify?token=hashedtoken&type=recovery"
    )
    result.properties.email_otp = "654321"
    return result


def _link_type(call):
    if call.args:
        params = call.args[0]
    else:
        params = call.kwargs.get("params", call.kwargs)
    return params.get("type")


def _csrf_from_html(html):
    match = re.search(
        r'<meta name="csrf-token" content="([^"]+)"',
        html,
    )
    assert match, "csrf meta tag missing"
    return match.group(1)


class TestPasswordRules(unittest.TestCase):
    def test_word_list_is_the_small_constant_set(self):
        module = _load_passwords()
        self.assertEqual(set(module.COMMON_PASSWORD_WORDS), REQUIRED_WORDS)

    def test_each_rule_returns_its_own_message(self):
        module = _load_passwords()
        for password, email, expected in PASSWORD_CASES:
            with self.subTest(password=password, email=email):
                self.assertEqual(module.password_error(password, email), expected)
                if expected:
                    self.assertNotIn(password, expected)
                    self.assertNotIn(password.strip(), expected)

    def test_seventy_two_utf8_bytes_is_the_limit(self):
        module = _load_passwords()
        ok = "Aa1!" + ("b" * 68)
        over = "Aa1!" + ("b" * 69)
        multi = "Aa1!" + ("\u00e9" * 35)
        self.assertEqual(len(ok.encode("utf-8")), 72)
        self.assertEqual(len(over.encode("utf-8")), 73)
        self.assertGreater(len(multi.encode("utf-8")), 72)
        self.assertIsNone(module.password_error(ok, "ada.lovelace@school.edu"))
        self.assertEqual(module.password_error(over, "ada.lovelace@school.edu"), MSG_LONG)
        self.assertEqual(module.password_error(multi, "ada.lovelace@school.edu"), MSG_LONG)


class TestPasswordRulesOnBothRoutes(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def _clear_limits(self):
        from app.routes import _rate_limit_events

        _rate_limit_events.clear()

    def _signup(self, password, email):
        self._clear_limits()
        shared = MagicMock()
        shared.auth.sign_up.return_value = MagicMock(user=MagicMock())
        payload = {
            "name": "Ada Lovelace",
            "email": email,
            "password": password,
            "invite_code": "",
        }
        with patch("app.routes.supabase", shared), patch(
            "app.routes.Config.IS_PRODUCTION", False
        ), patch(
            "app.routes.invite_code_is_valid", return_value=True
        ), patch(
            "app.routes.Config.PUBLIC_BASE_URL",
            "https://clarity.example",
            create=True,
        ):
            response = self.client.post("/api/signup", json=payload)
        return response, bool(shared.auth.sign_up.called), shared

    def _reset(self, password, email):
        self._clear_limits()
        with self.client.session_transaction() as sess:
            sess["csrf_token"] = "tok"
        verified = MagicMock()
        verified.user.id = "instructor-1"
        verified.user.email = email.strip().lower()
        verify = MagicMock(return_value=verified)
        admin = MagicMock()
        shared = MagicMock()
        admin.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"role": "instructor"}
        ]
        with patch(
            "app.routes.verify_recovery_token", verify
        ), patch("app.routes.supabase", shared), patch(
            "app.routes.supabase_admin", admin
        ), patch("app.routes.requests.post", return_value=_resend_ok()):
            response = self.client.post(
                "/api/reset-password",
                json={
                    "email": email,
                    "token_hash": "hashedtoken",
                    "password": password,
                },
                headers={"X-CSRF-Token": "tok"},
            )
        return response, verify, admin, shared

    def test_signup_and_reset_share_each_password_result(self):
        for password, email, expected in PASSWORD_CASES:
            with self.subTest(password=password, email=email):
                signup, signed_up, shared = self._signup(password, email)
                reset, verify, admin, reset_shared = self._reset(password, email)
                if expected is None:
                    self.assertEqual(signup.status_code, 200)
                    self.assertTrue(signup.get_json().get("success"))
                    self.assertTrue(signed_up)
                    self.assertEqual(reset.status_code, 200)
                    self.assertEqual(
                        reset.get_json().get("redirect"), "/login?reset=1"
                    )
                    verify.assert_called_once_with("hashedtoken")
                else:
                    self.assertEqual(signup.status_code, 400)
                    self.assertEqual(reset.status_code, 400)
                    self.assertEqual(signup.get_json()["message"], expected)
                    self.assertEqual(reset.get_json()["message"], expected)
                    self.assertNotIn(password, signup.get_json()["message"])
                    self.assertNotIn(password.strip(), reset.get_json()["message"])
                    self.assertFalse(signed_up)
                    verify.assert_not_called()
                    admin.auth.admin.update_user_by_id.assert_not_called()
                    shared.auth.verify_otp.assert_not_called()
                    reset_shared.auth.verify_otp.assert_not_called()


class TestForgotPassword(unittest.TestCase):
    def setUp(self):
        from app.routes import _rate_limit_events

        _rate_limit_events.clear()
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["csrf_token"] = "tok"

    def tearDown(self):
        from app.routes import _rate_limit_events

        _rate_limit_events.clear()

    def _post(self, email, admin):
        with patch("app.routes.supabase_admin", admin), patch(
            "app.routes.Config.PUBLIC_BASE_URL",
            "https://clarity.example",
            create=True,
        ), patch.dict(
            os.environ,
            {
                "RESEND_API_KEY": "re_test",
                "REPORTS_FROM_EMAIL": "reports@school.edu",
            },
            clear=False,
        ):
            sender = MagicMock(return_value=_resend_ok())
            with patch("app.routes.requests.post", sender):
                response = self.client.post(
                    "/api/forgot-password",
                    json={"email": email},
                    headers={"X-CSRF-Token": "tok"},
                )
        return response, sender

    def _assert_only_recovery(self, admin):
        calls = admin.auth.admin.generate_link.call_args_list
        self.assertTrue(calls)
        self.assertTrue(all(_link_type(call) == "recovery" for call in calls))
        tables = [call.args[0] for call in admin.table.call_args_list if call.args]
        self.assertNotIn("users", tables)

    def test_unknown_student_and_missing_profile_match_and_send_nothing(self):
        unknown_admin = MagicMock()
        unknown_admin.auth.admin.generate_link.side_effect = RuntimeError(
            "leaked-token-value"
        )
        with self.assertLogs("app.routes", level="ERROR") as leaked:
            unknown, unknown_send = self._post("missing@school.edu", unknown_admin)
        student_admin = MagicMock()
        student_admin.auth.admin.generate_link.return_value = _link(
            "student-1", "student@school.edu"
        )
        student_admin.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"role": "student"}
        ]
        student, student_send = self._post("student@school.edu", student_admin)
        empty_admin = MagicMock()
        empty_admin.auth.admin.generate_link.return_value = _link(
            "nobody-1", "nobody@school.edu"
        )
        empty_admin.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
        missing, missing_send = self._post("nobody@school.edu", empty_admin)

        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(unknown.get_json(), FORGOT_OK)
        self.assertEqual(student.get_json(), unknown.get_json())
        self.assertEqual(missing.get_json(), unknown.get_json())
        unknown_send.assert_not_called()
        student_send.assert_not_called()
        missing_send.assert_not_called()
        self._assert_only_recovery(unknown_admin)
        self._assert_only_recovery(student_admin)
        self._assert_only_recovery(empty_admin)
        self.assertNotIn("leaked-token-value", unknown.get_data(as_text=True))
        self.assertNotIn("leaked-token-value", "\n".join(leaked.output))
        self.assertNotIn("missing@school.edu", "\n".join(leaked.output))

    def test_instructor_link_uses_recovery_and_public_host(self):
        admin = MagicMock()
        admin.auth.admin.generate_link.return_value = _link()
        admin.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"role": "instructor"}
        ]
        response, sender = self._post("Ada.Lovelace@school.edu", admin)
        self.assertEqual(response.get_json(), FORGOT_OK)
        self._assert_only_recovery(admin)
        params = admin.auth.admin.generate_link.call_args.args[0]
        self.assertEqual(params["email"], "ada.lovelace@school.edu")
        self.assertEqual(
            params["options"]["redirect_to"],
            "https://clarity.example/reset-password",
        )
        payload = sender.call_args.kwargs["json"]
        self.assertEqual(payload["from"], '"Clarity Grader" <reports@school.edu>')
        self.assertNotIn("html", payload)
        self.assertNotIn("via Clarity Grader", payload["from"])
        expected = (
            "https://clarity.example/reset-password#token_hash=hashedtoken"
            "&type=recovery&email=" + quote("ada.lovelace@school.edu", safe="")
        )
        self.assertIn(expected, payload["text"])
        self.assertIn("https://clarity.example/reset-password#", payload["text"])
        self.assertNotIn("supabase", payload["text"].lower())
        self.assertNotIn("654321", payload["text"])
        self.assertNotIn("action_link", payload["text"])

    def test_public_base_url_ignores_request_host(self):
        from app.routes import _public_base_url

        with self.app.test_request_context(
            "/",
            base_url="http://evil.example",
            headers={"Host": "evil.example"},
        ), patch(
            "app.routes.Config.PUBLIC_BASE_URL",
            "https://clarity.example",
            create=True,
        ):
            self.assertEqual(_public_base_url(), "https://clarity.example")

    def test_empty_public_base_sends_nothing(self):
        admin = MagicMock()
        with patch("app.routes.supabase_admin", admin), patch(
            "app.routes.Config.PUBLIC_BASE_URL", "", create=True
        ):
            response = self.client.post(
                "/api/forgot-password",
                json={"email": "ada.lovelace@school.edu"},
                headers={"X-CSRF-Token": "tok"},
            )
        self.assertEqual(response.status_code, 403)
        admin.auth.admin.generate_link.assert_not_called()

    def test_missing_csrf_is_rejected_without_a_user(self):
        response = self.client.post(
            "/api/forgot-password",
            json={"email": "ada.lovelace@school.edu"},
        )
        self.assertEqual(response.status_code, 403)

    def test_ip_limit_and_email_limit(self):
        admin = MagicMock()
        admin.auth.admin.generate_link.side_effect = RuntimeError("absent")
        statuses = []
        for index in range(11):
            response, _sender = self._post("person%d@school.edu" % index, admin)
            statuses.append(response.status_code)
        self.assertEqual(statuses[:10], [200] * 10)
        self.assertEqual(statuses[10], 429)
        self.assertEqual(self.client.post(
            "/api/forgot-password",
            json={"email": "person11@school.edu"},
            headers={"X-CSRF-Token": "tok"},
        ).status_code, 429)

        from app.routes import _rate_limit_events

        _rate_limit_events.clear()
        email_statuses = []
        for _index in range(6):
            response, sender = self._post("same@school.edu", admin)
            email_statuses.append(response.status_code)
            sender.assert_not_called()
        self.assertEqual(email_statuses, [200, 200, 200, 200, 200, 429])
        self.assertEqual(
            self.client.post(
                "/api/forgot-password",
                json={"email": "same@school.edu"},
                headers={"X-CSRF-Token": "tok"},
            ).get_json(),
            RATE_LIMIT,
        )

    def test_account_send_cap_returns_the_generic_body(self):
        admin = MagicMock()
        admin.auth.admin.generate_link.return_value = _link()
        admin.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"role": "instructor"}
        ]
        sent = 0
        last = None
        for _index in range(4):
            response, sender = self._post("ada.lovelace@school.edu", admin)
            last = response
            if sender.called:
                sent += 1
        self.assertEqual(sent, 3)
        self.assertEqual(last.get_json(), FORGOT_OK)

    def test_idle_rate_limit_buckets_are_removed(self):
        from app.routes import _rate_limit, _rate_limit_bucket, _rate_limit_events

        _rate_limit_events.clear()
        stale = time.time() - 4000
        _rate_limit_events["forgot_password_email:old@school.edu"] = deque([stale])
        _rate_limit_events["forgot_password_email:also@school.edu"] = deque([stale])
        _rate_limit_events["empty-bucket"] = deque()
        self.assertTrue(_rate_limit_bucket("probe", 5, 60))
        self.assertNotIn("forgot_password_email:old@school.edu", _rate_limit_events)
        self.assertNotIn("forgot_password_email:also@school.edu", _rate_limit_events)
        self.assertNotIn("empty-bucket", _rate_limit_events)
        self.assertIn("probe", _rate_limit_events)
        with self.app.test_request_context("/"):
            self.assertTrue(_rate_limit("api_login", 20, 300))
        self.assertTrue(
            any(key.startswith("api_login:") for key in _rate_limit_events)
        )


class TestResetPassword(unittest.TestCase):
    def setUp(self):
        from app.routes import _rate_limit_events

        _rate_limit_events.clear()
        clear_app_session_cache()
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        from app.routes import _rate_limit_events

        _rate_limit_events.clear()
        clear_app_session_cache()

    def _admin(self):
        admin = MagicMock()
        admin.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"role": "instructor"}
        ]
        return admin

    def _post(self, admin, sender, password="Aa1!bbbbbbbb"):
        with self.client.session_transaction() as sess:
            sess["csrf_token"] = "tok"
        verified = MagicMock()
        verified.user.id = "instructor-1"
        verified.user.email = "ada.lovelace@school.edu"
        verify = MagicMock(return_value=verified)
        with patch(
            "app.routes.verify_recovery_token", verify
        ), patch("app.routes.supabase_admin", admin), patch(
            "app.routes.requests.post", sender
        ), patch.dict(
            os.environ,
            {
                "RESEND_API_KEY": "re_test",
                "REPORTS_FROM_EMAIL": "reports@school.edu",
            },
            clear=False,
        ):
            response = self.client.post(
                "/api/reset-password",
                json={
                    "email": "ada.lovelace@school.edu",
                    "token_hash": "hashedtoken",
                    "password": password,
                },
                headers={"X-CSRF-Token": "tok"},
            )
        return response, verify

    def test_success_revokes_sessions_and_sends_notice(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        remember_app_session("hash-a", "instructor-1", future, 1000000.0)
        remember_app_session("hash-b", "instructor-1", future, 1000000.0)
        remember_app_session("hash-c", "other-user", future, 1000000.0)
        admin = self._admin()
        response, verify = self._post(admin, MagicMock(return_value=_resend_ok()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "success": True,
            "redirect": "/login?reset=1",
        })
        verify.assert_called_once_with("hashedtoken")
        admin.auth.admin.update_user_by_id.assert_called_once()
        update_args = admin.auth.admin.update_user_by_id.call_args.args
        self.assertEqual(update_args[0], "instructor-1")
        self.assertEqual(update_args[1]["password"], "Aa1!bbbbbbbb")
        admin.table.return_value.update.return_value.eq.assert_any_call(
            "user_id", "instructor-1"
        )
        self.assertIsNone(cached_app_session("hash-a", 1000000.0))
        self.assertIsNone(cached_app_session("hash-b", 1000000.0))
        self.assertIsNotNone(cached_app_session("hash-c", 1000000.0))

    def test_notice_uses_the_clarity_grader_from_line(self):
        admin = self._admin()
        sender = MagicMock(return_value=_resend_ok())
        response, _verify = self._post(admin, sender)
        self.assertEqual(response.status_code, 200)
        payload = sender.call_args.kwargs["json"]
        self.assertEqual(payload["from"], '"Clarity Grader" <reports@school.edu>')
        self.assertEqual(payload["subject"], "Your Clarity Grader password was changed")
        self.assertNotIn("html", payload)
        self.assertNotIn("http", payload["text"].lower())
        self.assertIn("contact the site owner", payload["text"].lower())
        self.assertNotIn("hashedtoken", payload["text"])

    def test_notice_failure_is_logged_without_the_address_and_reset_succeeds(self):
        admin = self._admin()
        sender = MagicMock(
            side_effect=RuntimeError("provider body ada.lovelace@school.edu leaked")
        )
        with self.assertLogs("app.routes", level="ERROR") as logs:
            response, verify = self._post(admin, sender)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["redirect"], "/login?reset=1")
        verify.assert_called_once()
        joined = "\n".join(logs.output)
        self.assertNotIn("ada.lovelace@school.edu", joined)
        self.assertNotIn("provider body", joined)
        self.assertNotIn("hashedtoken", joined)

    def test_update_failure_returns_save_message(self):
        admin = self._admin()
        admin.auth.admin.update_user_by_id.side_effect = RuntimeError("provider")
        with self.assertLogs("app.routes", level="ERROR") as logs:
            response, verify = self._post(admin, MagicMock(return_value=_resend_ok()))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], MSG_SAVE)
        verify.assert_called_once()
        self.assertNotIn("provider", "\n".join(logs.output))

    def test_ip_limit(self):
        admin = self._admin()
        statuses = []
        for _index in range(11):
            response, _verify = self._post(admin, MagicMock(return_value=_resend_ok()))
            statuses.append(response.status_code)
        self.assertEqual(statuses[:10], [200] * 10)
        self.assertEqual(statuses[10], 429)

    def test_dead_cookie_can_still_reset(self):
        with self.client.session_transaction() as sess:
            sess["user_id"] = "user-1"
            sess["csrf_token"] = "tok"
            sess["session_token"] = "absent-token"
        admin = self._admin()
        response, verify = self._post(admin, MagicMock(return_value=_resend_ok()))
        self.assertEqual(response.status_code, 200)
        verify.assert_called_once()
        self.assertNotEqual(response.get_json().get("code"), "session_expired")


def _requirements_list(path):
    text = Path(path).read_text(encoding="utf-8")
    marker = (
        '<ul class="list-disc list-inside space-y-1 text-xs '
        'text-slate-500 dark:text-slate-400 mb-1.5">'
    )
    start = text.index(marker)
    depth = 0
    index = start
    while index < len(text):
        open_at = text.find("<ul", index)
        close_at = text.find("</ul>", index)
        if close_at < 0:
            break
        if open_at != -1 and open_at < close_at:
            depth += 1
            index = open_at + 3
            continue
        depth -= 1
        index = close_at + len("</ul>")
        if depth == 0:
            return text[start:index]
    raise AssertionError("requirements list is unclosed in %s" % path)


class TestResetPages(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_login_links_to_forgot_password_and_shows_the_updated_notice(self):
        login = self.client.get("/login")
        self.assertIn(b"Forgot password?", login.data)
        self.assertIn(b'href="/forgot-password"', login.data)
        self.assertNotIn(b"passwordResetNotice", login.data)
        reset = self.client.get("/login?reset=1")
        self.assertIn(b'id="passwordResetNotice"', reset.data)
        self.assertIn(b"Your password was updated. Sign in with the new password.", reset.data)

    def test_forgot_and_reset_pages_ignore_a_dead_session(self):
        with self.client.session_transaction() as sess:
            sess["user_id"] = "user-1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "tok"
            sess["session_token"] = "absent-token"
        empty = MagicMock()
        empty.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
        with patch("app.routes.supabase_admin", empty):
            forgot = self.client.get("/forgot-password")
            reset = self.client.get("/reset-password")
        self.assertEqual(forgot.status_code, 200)
        self.assertEqual(reset.status_code, 200)

    def test_reset_page_script_reads_the_fragment_and_drops_it(self):
        path = Path("app/templates/reset_password.html")
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertLess(text.index("At least 12 characters"), text.index('id="password"'))
        self.assertIn("At least 12 characters", text)
        self.assertIn("Must contain one of each:", text)
        self.assertIn("Lowercase letter", text)
        self.assertIn("Uppercase letter", text)
        self.assertIn("Digit", text)
        self.assertIn("One symbol, such as ! or #", text)
        self.assertIn("location.hash", text)
        self.assertIn("history.replaceState", text)
        self.assertIn('params.get("token_hash")', text)
        self.assertIn('params.get("email")', text)
        self.assertIn('fetch("/api/reset-password"', text)
        self.assertIn('addEventListener("input"', text)
        self.assertIn("value.length < 12", text)
        self.assertIn("encoder.encode(value).length > 72", text)
        self.assertIn("/[a-z]/", text)
        self.assertIn("/[A-Z]/", text)
        self.assertIn("/[0-9]/", text)
        self.assertIn("[^A-Za-z0-9\\s]", text)
        self.assertNotIn("/api/reset-password?", text)
        self.assertNotIn('type="email"', text)
        self.assertNotIn('id="email"', text)

    def test_emailed_link_contains_quoted_email_and_page_has_no_email_input(self):
        admin = MagicMock()
        admin.auth.admin.generate_link.return_value = _link()
        admin.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"role": "instructor"}
        ]
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["csrf_token"] = "tok"
        from app.routes import _rate_limit_events

        _rate_limit_events.clear()
        with patch("app.routes.supabase_admin", admin), patch(
            "app.routes.Config.PUBLIC_BASE_URL",
            "https://clarity.example",
            create=True,
        ), patch.dict(
            os.environ,
            {
                "RESEND_API_KEY": "re_test",
                "REPORTS_FROM_EMAIL": "reports@school.edu",
            },
            clear=False,
        ), patch("app.routes.requests.post", return_value=_resend_ok()) as sender:
            response = client.post(
                "/api/forgot-password",
                json={"email": "Ada.Lovelace@school.edu"},
                headers={"X-CSRF-Token": "tok"},
            )
        self.assertEqual(response.status_code, 200)
        body = sender.call_args.kwargs["json"]["text"]
        self.assertIn(
            "&email=" + quote("ada.lovelace@school.edu", safe=""),
            body,
        )
        page = client.get("/reset-password")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b'type="email"', page.data)
        self.assertNotIn(b'id="email"', page.data)

    def test_get_reset_does_not_call_verify_or_generate(self):
        admin = MagicMock()
        with patch("app.routes.supabase_admin", admin), patch(
            "app.routes.verify_recovery_token"
        ) as verify:
            response = self.client.get("/reset-password")
        self.assertEqual(response.status_code, 200)
        verify.assert_not_called()
        admin.auth.admin.generate_link.assert_not_called()
        admin.auth.admin.update_user_by_id.assert_not_called()
        self.assertNotIn(b"hashedtoken", response.data)

    def test_signup_page_shows_requirements_and_reads_message(self):
        page = self.client.get("/signup")
        self.assertEqual(page.status_code, 200)
        text = page.get_data(as_text=True)
        self.assertIn("At least 12 characters", text)
        self.assertIn("Must contain one of each:", text)
        self.assertIn("Lowercase letter", text)
        self.assertIn("Uppercase letter", text)
        self.assertIn("Digit", text)
        self.assertIn("One symbol, such as ! or #", text)
        self.assertIn("res.message", text)
        self.assertIn("showError(msg", text)

    def test_signup_and_reset_requirements_lists_match(self):
        signup = _requirements_list("app/templates/signup.html")
        reset = _requirements_list("app/templates/reset_password.html")
        self.assertEqual(signup, reset)


class TestPasswordResetCsrf(unittest.TestCase):
    def setUp(self):
        from app.routes import _rate_limit_events

        _rate_limit_events.clear()
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        from app.routes import _rate_limit_events

        _rate_limit_events.clear()

    def test_anonymous_pages_mint_csrf_and_posts_require_it(self):
        forgot_page = self.client.get("/forgot-password")
        reset_page = self.client.get("/reset-password")
        self.assertEqual(forgot_page.status_code, 200)
        self.assertEqual(reset_page.status_code, 200)
        forgot_token = _csrf_from_html(forgot_page.get_data(as_text=True))
        reset_token = _csrf_from_html(reset_page.get_data(as_text=True))
        self.assertTrue(forgot_token)
        self.assertTrue(reset_token)

        denied_forgot = self.client.post(
            "/api/forgot-password",
            json={"email": "ada.lovelace@school.edu"},
        )
        denied_reset = self.client.post(
            "/api/reset-password",
            json={
                "email": "ada.lovelace@school.edu",
                "token_hash": "hashedtoken",
                "password": "Aa1!bbbbbbbb",
            },
        )
        self.assertEqual(denied_forgot.status_code, 403)
        self.assertEqual(denied_reset.status_code, 403)

        admin = MagicMock()
        admin.auth.admin.generate_link.side_effect = RuntimeError("absent")
        with patch("app.routes.supabase_admin", admin), patch(
            "app.routes.Config.PUBLIC_BASE_URL",
            "https://clarity.example",
            create=True,
        ):
            ok_forgot = self.client.post(
                "/api/forgot-password",
                json={"email": "ada.lovelace@school.edu"},
                headers={"X-CSRF-Token": forgot_token},
            )
        self.assertEqual(ok_forgot.status_code, 200)
        self.assertEqual(ok_forgot.get_json(), FORGOT_OK)

        verified = MagicMock()
        verified.user.id = "instructor-1"
        verified.user.email = "ada.lovelace@school.edu"
        reset_admin = MagicMock()
        reset_admin.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"role": "instructor"}
        ]
        with patch(
            "app.routes.verify_recovery_token",
            MagicMock(return_value=verified),
        ), patch("app.routes.supabase_admin", reset_admin), patch(
            "app.routes.requests.post", return_value=_resend_ok()
        ), patch.dict(
            os.environ,
            {
                "RESEND_API_KEY": "re_test",
                "REPORTS_FROM_EMAIL": "reports@school.edu",
            },
            clear=False,
        ):
            ok_reset = self.client.post(
                "/api/reset-password",
                json={
                    "email": "ada.lovelace@school.edu",
                    "token_hash": "hashedtoken",
                    "password": "Aa1!bbbbbbbb",
                },
                headers={"X-CSRF-Token": reset_token},
            )
        self.assertEqual(ok_reset.status_code, 200)
        self.assertEqual(ok_reset.get_json()["redirect"], "/login?reset=1")


class TestVerifyRecoveryToken(unittest.TestCase):
    def test_builds_a_fresh_client_from_authentication_url_and_key(self):
        import importlib
        import sys

        from app.routes import verify_recovery_token

        installed = sys.modules.get("app.authentication")
        fn = getattr(installed, "new_anon_client", None)
        swapped = getattr(fn, "__module__", None) != "app.authentication"
        if swapped:
            sys.modules.pop("app.authentication", None)
            app_pkg = sys.modules.get("app")
            if app_pkg is not None and "authentication" in app_pkg.__dict__:
                del app_pkg.__dict__["authentication"]
            importlib.import_module("app.authentication")
        try:
            first = MagicMock()
            second = MagicMock()
            first.auth.verify_otp.return_value = MagicMock()
            second.auth.verify_otp.return_value = MagicMock()
            shared = MagicMock()
            with patch(
                "app.authentication.create_client",
                side_effect=[first, second],
            ) as built, patch(
                "app.authentication.supabase", shared
            ), patch("app.routes.supabase", shared):
                one = verify_recovery_token("hashedtoken")
                two = verify_recovery_token("other-token")
            import app.authentication as auth

            self.assertEqual(built.call_count, 2)
            self.assertEqual(built.call_args_list[0].args, (auth.url, auth.key))
            self.assertEqual(built.call_args_list[1].args, (auth.url, auth.key))
            self.assertIs(one, first.auth.verify_otp.return_value)
            self.assertIs(two, second.auth.verify_otp.return_value)
            first.auth.verify_otp.assert_called_once_with(
                {"token_hash": "hashedtoken", "type": "recovery"}
            )
            second.auth.verify_otp.assert_called_once_with(
                {"token_hash": "other-token", "type": "recovery"}
            )
            shared.auth.verify_otp.assert_not_called()
        finally:
            if swapped and installed is not None:
                sys.modules["app.authentication"] = installed
                app_pkg = sys.modules.get("app")
                if app_pkg is not None:
                    app_pkg.authentication = installed


class TestResetRevokeOrder(unittest.TestCase):
    def setUp(self):
        from app.routes import _rate_limit_events

        _rate_limit_events.clear()
        clear_app_session_cache()
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        from app.routes import _rate_limit_events

        _rate_limit_events.clear()
        clear_app_session_cache()

    def _post(self, admin, revoke):
        with self.client.session_transaction() as sess:
            sess["csrf_token"] = "tok"
        verified = MagicMock()
        verified.user.id = "instructor-1"
        verified.user.email = "ada.lovelace@school.edu"
        with patch(
            "app.routes.verify_recovery_token",
            MagicMock(return_value=verified),
        ), patch("app.routes.supabase_admin", admin), patch(
            "app.app_sessions.revoke_user_sessions", revoke
        ), patch(
            "app.routes.requests.post", return_value=_resend_ok()
        ), patch.dict(
            os.environ,
            {
                "RESEND_API_KEY": "re_test",
                "REPORTS_FROM_EMAIL": "reports@school.edu",
            },
            clear=False,
        ):
            return self.client.post(
                "/api/reset-password",
                json={
                    "email": "ada.lovelace@school.edu",
                    "token_hash": "hashedtoken",
                    "password": "Aa1!bbbbbbbb",
                },
                headers={"X-CSRF-Token": "tok"},
            )

    def test_revoke_runs_before_update(self):
        admin = MagicMock()
        admin.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"role": "instructor"}
        ]
        order = []

        def track_revoke(user_id):
            order.append(("revoke", user_id))

        admin.auth.admin.update_user_by_id.side_effect = (
            lambda user_id, body: order.append(("update", user_id))
        )
        response = self._post(admin, track_revoke)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            order,
            [("revoke", "instructor-1"), ("update", "instructor-1")],
        )

    def test_revoke_failure_returns_500_and_skips_update(self):
        admin = MagicMock()
        admin.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"role": "instructor"}
        ]

        def boom(user_id):
            raise RuntimeError("session store down")

        with self.assertLogs("app.routes", level="ERROR") as logs:
            response = self._post(admin, boom)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["message"], MSG_SAVE)
        admin.auth.admin.update_user_by_id.assert_not_called()
        joined = "\n".join(logs.output)
        self.assertIn("RuntimeError", joined)
        self.assertNotIn("instructor-1", joined)
        self.assertNotIn("ada.lovelace@school.edu", joined)
        self.assertNotIn("session store down", joined)


class TestRevokeUserSessionsFilter(unittest.TestCase):
    def test_already_revoked_rows_are_not_updated(self):
        from app.app_sessions import revoke_user_sessions

        admin = MagicMock()
        query = admin.table.return_value.update.return_value.eq.return_value
        with patch("app.app_sessions._admin", return_value=admin), patch(
            "app.app_sessions._cache", {"live": (0.0, "instructor-1", None)}
        ):
            revoke_user_sessions("instructor-1")
        admin.table.assert_called_with("app_sessions")
        admin.table.return_value.update.return_value.eq.assert_called_with(
            "user_id", "instructor-1"
        )
        query.is_.assert_called_once_with("revoked_at", "null")
        query.is_.return_value.execute.assert_called_once()


class TestResetPasswordGate(unittest.TestCase):
    def setUp(self):
        from app.routes import _rate_limit_events

        _rate_limit_events.clear()
        clear_app_session_cache()
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        from app.routes import _rate_limit_events

        _rate_limit_events.clear()
        clear_app_session_cache()

    def _post(self, *, password, verify, admin):
        with self.client.session_transaction() as sess:
            sess["csrf_token"] = "tok"
        with patch("app.routes.verify_recovery_token", verify), patch(
            "app.routes.supabase_admin", admin
        ), patch(
            "app.app_sessions.revoke_user_sessions", MagicMock()
        ), patch(
            "app.routes.requests.post", return_value=_resend_ok()
        ), patch.dict(
            os.environ,
            {
                "RESEND_API_KEY": "re_test",
                "REPORTS_FROM_EMAIL": "reports@school.edu",
            },
            clear=False,
        ):
            return self.client.post(
                "/api/reset-password",
                json={
                    "email": "ada.lovelace@school.edu",
                    "token_hash": "hashedtoken",
                    "password": password,
                },
                headers={"X-CSRF-Token": "tok"},
            )

    def test_password_rule_failures_do_not_consume_the_ip_limit(self):
        admin = MagicMock()
        verify = MagicMock(side_effect=AssertionError("verify must not run"))
        statuses = []
        for _index in range(11):
            response = self._post(
                password="short",
                verify=verify,
                admin=admin,
            )
            statuses.append(response.status_code)
        self.assertEqual(statuses, [400] * 11)
        verify.assert_not_called()
        admin.auth.admin.update_user_by_id.assert_not_called()

        admin.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"role": "instructor"}
        ]
        verified = MagicMock()
        verified.user.id = "instructor-1"
        verified.user.email = "ada.lovelace@school.edu"
        verify_ok = MagicMock(return_value=verified)
        valid_statuses = []
        for _index in range(11):
            response = self._post(
                password="Aa1!bbbbbbbb",
                verify=verify_ok,
                admin=admin,
            )
            valid_statuses.append(response.status_code)
        self.assertEqual(valid_statuses[:10], [200] * 10)
        self.assertEqual(valid_statuses[10], 429)

    def test_expired_token_skips_update_and_revoke(self):
        admin = MagicMock()
        revoke = MagicMock()
        verify = MagicMock(side_effect=RuntimeError("expired"))
        with patch("app.app_sessions.revoke_user_sessions", revoke):
            with self.assertLogs("app.routes", level="ERROR"):
                response = self._post(
                    password="Aa1!bbbbbbbb",
                    verify=verify,
                    admin=admin,
                )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["message"],
            "This reset link is invalid or expired.",
        )
        admin.auth.admin.update_user_by_id.assert_not_called()
        revoke.assert_not_called()

    def test_email_mismatch_skips_update(self):
        admin = MagicMock()
        admin.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"role": "instructor"}
        ]
        verified = MagicMock()
        verified.user.id = "instructor-1"
        verified.user.email = "other@school.edu"
        revoke = MagicMock()
        with patch("app.app_sessions.revoke_user_sessions", revoke):
            response = self._post(
                password="Aa1!bbbbbbbb",
                verify=MagicMock(return_value=verified),
                admin=admin,
            )
        self.assertEqual(response.status_code, 400)
        admin.auth.admin.update_user_by_id.assert_not_called()
        revoke.assert_not_called()

    def test_non_instructor_skips_update(self):
        admin = MagicMock()
        admin.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"role": "student"}
        ]
        verified = MagicMock()
        verified.user.id = "student-1"
        verified.user.email = "ada.lovelace@school.edu"
        revoke = MagicMock()
        with patch("app.app_sessions.revoke_user_sessions", revoke):
            response = self._post(
                password="Aa1!bbbbbbbb",
                verify=MagicMock(return_value=verified),
                admin=admin,
            )
        self.assertEqual(response.status_code, 400)
        admin.auth.admin.update_user_by_id.assert_not_called()
        revoke.assert_not_called()


class TestForgotPasswordHostHeader(unittest.TestCase):
    def setUp(self):
        from app.routes import _rate_limit_events

        _rate_limit_events.clear()
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        from app.routes import _rate_limit_events

        _rate_limit_events.clear()

    def test_resend_link_ignores_evil_host(self):
        page = self.client.get(
            "/forgot-password",
            base_url="http://evil.example",
            headers={"Host": "evil.example"},
        )
        self.assertEqual(page.status_code, 200)
        token = _csrf_from_html(page.get_data(as_text=True))
        admin = MagicMock()
        admin.auth.admin.generate_link.return_value = _link()
        admin.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"role": "instructor"}
        ]
        sender = MagicMock(return_value=_resend_ok())
        with patch("app.routes.supabase_admin", admin), patch(
            "app.routes.Config.PUBLIC_BASE_URL",
            "https://clarity.example",
            create=True,
        ), patch.dict(
            os.environ,
            {
                "RESEND_API_KEY": "re_test",
                "REPORTS_FROM_EMAIL": "reports@school.edu",
            },
            clear=False,
        ), patch("app.routes.requests.post", sender):
            response = self.client.post(
                "/api/forgot-password",
                json={"email": "ada.lovelace@school.edu"},
                headers={"X-CSRF-Token": token, "Host": "evil.example"},
                base_url="http://evil.example",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), FORGOT_OK)
        params = admin.auth.admin.generate_link.call_args.args[0]
        self.assertEqual(
            params["options"]["redirect_to"],
            "https://clarity.example/reset-password",
        )
        body = sender.call_args.kwargs["json"]["text"]
        self.assertIn("https://clarity.example/reset-password", body)
        self.assertNotIn("evil.example", body)


class TestRateLimitIdleWindow(unittest.TestCase):
    def setUp(self):
        from app.routes import _rate_limit_events

        _rate_limit_events.clear()

    def tearDown(self):
        from app.routes import _rate_limit_events

        _rate_limit_events.clear()

    def test_window_longer_than_idle_limit_is_rejected(self):
        from app.routes import _RATE_LIMIT_MAX_IDLE, _rate_limit_bucket

        self.assertEqual(_RATE_LIMIT_MAX_IDLE, 3600)
        with self.assertRaises(ValueError):
            _rate_limit_bucket("too-long", 5, 3601)
        self.assertTrue(_rate_limit_bucket("ok-window", 5, 3600))


class TestResetLogging(unittest.TestCase):
    def test_password_module_does_not_log(self):
        module = _load_passwords()
        logger = logging.getLogger("app.passwords")
        with self.assertNoLogs(logger, level="DEBUG"):
            module.password_error("Aa1!bbbbbbbb", "ada.lovelace@school.edu")
