"""Reports page loads jsPDF from this app, not from a CDN."""

import unittest

from app import create_app


class TestVendoredPdfScripts(unittest.TestCase):
    def _reports_html(self):
        from flask import render_template, session

        app = create_app()
        with app.test_request_context("/"):
            session["role"] = "instructor"
            return render_template(
                "class_reports.html",
                class_id="c1",
                class_name="Test Upload",
                class_days="MW",
                class_section="F26",
                students=[],
                learning_objectives=[],
                assignments=[],
                instructor_title="Professor Estes",
            )

    def test_reports_page_has_no_jsdelivr_url(self):
        html = self._reports_html()
        offending = [line.strip() for line in html.splitlines() if "cdn.jsdelivr.net" in line]
        self.assertEqual(offending, [])
        self.assertIn("/static/js/vendor/jspdf.umd.min.js", html)
        self.assertIn("/static/js/vendor/jspdf.plugin.autotable.min.js", html)
        self.assertLess(
            html.find("jspdf.umd.min.js"),
            html.find("jspdf.plugin.autotable.min.js"),
        )

    def test_vendor_scripts_are_served_as_javascript(self):
        app = create_app()
        client = app.test_client()
        for path in (
            "/static/js/vendor/jspdf.umd.min.js",
            "/static/js/vendor/jspdf.plugin.autotable.min.js",
        ):
            response = client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn("javascript", response.content_type)


if __name__ == "__main__":
    unittest.main()
