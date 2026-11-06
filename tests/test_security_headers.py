"""Enforced Content-Security-Policy header."""

import unittest

from app import create_app

ENFORCED_CSP = "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; font-src 'self'; connect-src 'self'"


def _script_src(policy):
    for part in policy.split(";"):
        directive = part.strip()
        if directive == "script-src" or directive.startswith("script-src "):
            return directive
    return ""


class TestSecurityHeaders(unittest.TestCase):
    def setUp(self):
        self.client = create_app().test_client()

    def _assert_csp(self, response):
        self.assertEqual(response.headers["Content-Security-Policy"], ENFORCED_CSP)
        self.assertNotIn("Content-Security-Policy-Report-Only", response.headers)
        script_src = _script_src(response.headers["Content-Security-Policy"])
        self.assertEqual(script_src, "script-src 'self'")
        self.assertIn("'self'", script_src)
        self.assertNotIn("unsafe-inline", script_src)
        self.assertNotIn("unsafe-eval", script_src)

    def test_login_sends_the_enforced_policy_and_existing_headers(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self._assert_csp(response)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertEqual(
            response.headers["Permissions-Policy"],
            "camera=(), microphone=(), geolocation=()",
        )

    def test_protected_redirect_carries_the_enforced_policy(self):
        response = self.client.get("/instructor/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        self._assert_csp(response)

    def test_json_api_response_carries_the_enforced_policy(self):
        response = self.client.get("/api/session-status")
        self.assertEqual(response.status_code, 401)
        self.assertIn("json", response.content_type)
        self.assertIsInstance(response.get_json(), dict)
        self._assert_csp(response)

    def test_missing_page_carries_the_enforced_policy(self):
        response = self.client.get("/no-such-page")
        self.assertEqual(response.status_code, 404)
        self._assert_csp(response)

    def test_static_file_carries_the_enforced_policy(self):
        response = self.client.get("/static/js/login.js")
        self.assertEqual(response.status_code, 200)
        self._assert_csp(response)


if __name__ == "__main__":
    unittest.main()
