import os
import unittest
import unittest.mock

from app import create_app


class TestPublicRoutes(unittest.TestCase):
    """Verify public pages are reachable without authentication."""

    def setUp(self):
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def test_login_page_returns_200(self):
        response = self.client.get('/login')
        self.assertEqual(response.status_code, 200)

    def test_root_returns_login(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)

    def test_signup_page_returns_200(self):
        response = self.client.get('/signup')
        self.assertEqual(response.status_code, 200)

    def test_logout_redirects(self):
        response = self.client.get('/logout')
        self.assertEqual(response.status_code, 405)


class TestSignupEmailRedirect(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.payload = {
            "name": "Ada Lovelace",
            "email": "ada@example.com",
            "password": "Correct-horse1!",
            "invite_code": "",
        }

    def _post(self, *, base_url, public_base, headers=None, production=False):
        from app.helpers.authentication import supabase
        supabase.auth.sign_up.return_value = unittest.mock.MagicMock(user=None)
        supabase.auth.sign_up.side_effect = None
        supabase.auth.sign_up.reset_mock()
        with unittest.mock.patch("config.Config.IS_PRODUCTION", production), \
                unittest.mock.patch(
                    "app.routes.auth.invite_code_is_valid", return_value=True
                ), \
                unittest.mock.patch(
                    "config.Config.PUBLIC_BASE_URL", public_base, create=True
                ), \
                unittest.mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": ""}, clear=False):
            response = self.client.post(
                "/api/signup",
                json=self.payload,
                base_url=base_url,
                headers=headers or {},
            )
        return response, supabase.auth.sign_up

    def test_missing_public_base_does_not_use_the_request_host(self):
        response, sign_up = self._post(
            base_url="http://grader.example.com",
            public_base="",
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(response.status_code, 403)
        sign_up.assert_not_called()

    def test_public_base_url_wins(self):
        response, sign_up = self._post(
            base_url="http://grader.example.com",
            public_base="https://clarity.example",
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            sign_up.call_args.args[0]["options"]["email_redirect_to"],
            "https://clarity.example/login",
        )

    def test_localhost_without_public_base_does_not_call_supabase(self):
        response, sign_up = self._post(
            base_url="http://localhost:5000",
            public_base="",
        )
        self.assertEqual(response.status_code, 403)
        sign_up.assert_not_called()

    def test_evil_host_uses_only_public_base_url(self):
        response, sign_up = self._post(
            base_url="http://evil.example",
            public_base="https://clarity.example",
            headers={"Host": "evil.example"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            sign_up.call_args.args[0]["options"]["email_redirect_to"],
            "https://clarity.example/login",
        )


class TestLoginRole(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def _login(self, profile_result):
        from app.helpers.authentication import supabase, supabase_admin
        user = unittest.mock.MagicMock()
        user.id = "user-1"
        user.user_metadata = {"role": "/evil.example", "full_name": "Ada"}
        result = unittest.mock.MagicMock(user=user)
        supabase.auth.sign_in_with_password.side_effect = None
        supabase.auth.sign_in_with_password.return_value = result
        execute = (
            supabase_admin.table.return_value
            .select.return_value
            .eq.return_value
            .limit.return_value
            .execute
        )
        if isinstance(profile_result, Exception):
            execute.side_effect = profile_result
        else:
            execute.side_effect = None
            execute.return_value = profile_result
        supabase_admin.table.return_value.upsert.reset_mock()
        response = self.client.post(
            "/api/login",
            json={"email": "ada@example.com", "password": "secret"},
        )
        return response, supabase_admin

    def test_profile_read_error_does_not_open_a_session(self):
        response, admin = self._login(RuntimeError("db down"))
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("redirect", response.get_json())
        with self.client.session_transaction() as sess:
            self.assertNotIn("user_id", sess)
        admin.table.return_value.upsert.assert_not_called()

    def test_non_instructor_role_is_refused(self):
        for role in ("student", "/evil.example", ""):
            response, admin = self._login(
                unittest.mock.MagicMock(data=[{"role": role}])
            )
            self.assertEqual(response.status_code, 401, role)
            self.assertNotIn("redirect", response.get_json())
            with self.client.session_transaction() as sess:
                self.assertNotIn("user_id", sess)
            admin.table.return_value.upsert.assert_not_called()

    def test_instructor_redirect_is_the_fixed_path(self):
        response, admin = self._login(
            unittest.mock.MagicMock(data=[{"role": "Instructor"}])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["redirect"], "/instructor/dashboard")
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["role"], "instructor")
        admin.table.return_value.upsert.assert_not_called()
