"""Enforced and report-only Content-Security-Policy headers."""

import unittest

from app import create_app

ENFORCED_CSP = "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
REPORT_ONLY_CSP = "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; font-src 'self'; connect-src 'self'"


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
        self.assertEqual(
            response.headers["Content-Security-Policy-Report-Only"],
            REPORT_ONLY_CSP,
        )

    def test_login_sends_both_policies_and_existing_headers(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self._assert_csp(response)
        enforced = response.headers["Content-Security-Policy"]
        report_only = response.headers["Content-Security-Policy-Report-Only"]
        self.assertNotIn("'unsafe-eval'", enforced)
        self.assertNotIn("'unsafe-eval'", report_only)
        script_src = _script_src(report_only)
        self.assertTrue(script_src.startswith("script-src"))
        self.assertNotIn("'unsafe-inline'", script_src)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertEqual(
            response.headers["Permissions-Policy"],
            "camera=(), microphone=(), geolocation=()",
        )

    def test_protected_redirect_carries_both_csp_headers(self):
        response = self.client.get("/instructor/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        self._assert_csp(response)

    def test_json_api_response_carries_both_csp_headers(self):
        response = self.client.get("/api/session-status")
        self.assertEqual(response.status_code, 401)
        self.assertIn("json", response.content_type)
        self.assertIsInstance(response.get_json(), dict)
        self._assert_csp(response)


if __name__ == "__main__":
    unittest.main()
