"""Pages must load Tailwind from this app, and the build must include dynamic classes."""

import re
import unittest
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

from app import create_app
from app.routes import build_revision_report

ROOT = Path(__file__).resolve().parents[1]
CSS_PATH = ROOT / "app" / "static" / "css" / "tailwind.css"
VENDOR_DIR = ROOT / "app" / "static" / "js" / "vendor"

DYNAMIC_UTILITIES = (
    "bg-blue-100",
    "dark:bg-blue-900/40",
    "text-blue-700",
    "dark:text-blue-300",
    "text-blue-800",
    "bg-slate-100",
    "dark:bg-slate-700",
    "text-slate-700",
    "dark:text-slate-300",
    "text-slate-600",
    "bg-green-100",
    "dark:bg-green-900/40",
    "text-green-700",
    "dark:text-green-300",
    "bg-amber-100",
    "dark:bg-amber-900/40",
    "text-amber-800",
    "dark:text-amber-200",
    "dark:text-amber-300",
    "bg-red-100",
    "dark:bg-red-900/40",
    "text-red-700",
    "dark:text-red-300",
    "bg-sky-600",
    "bg-blue-600",
    "text-white",
    "hover:bg-slate-200",
    "dark:hover:bg-slate-600",
    "p-0.5",
    "p-1",
    "w-3.5",
    "h-3.5",
)
_SINGLE_WORD_UTILITIES = {
    "flex", "grid", "block", "inline", "hidden", "border", "rounded",
    "shadow", "relative", "absolute", "fixed", "sticky", "truncate",
    "uppercase", "lowercase", "capitalize", "italic", "underline",
    "transition", "container", "invisible", "static",
}

MASTERY_RULES = (
    "mastery-m",
    "mastery-mr",
    "mastery-r",
    "mastery-rq",
    "mastery-p",
    "mastery-x",
    "mastery-a",
    "mastery-i",
)


def _selector(token):
    escaped = (
        token.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("/", "\\/")
        .replace(".", "\\.")
    )
    return "." + escaped


class _AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.urls.append(("script", values["src"]))
        if tag == "link":
            rel = str(values.get("rel") or "").lower().split()
            href = values.get("href")
            if href and "stylesheet" in rel:
                self.urls.append(("stylesheet", href))


def foreign_asset_urls(html):
    parser = _AssetParser()
    parser.feed(html)
    found = []
    for kind, url in parser.urls:
        if url.startswith(("http://", "https://", "//")):
            found.append("%s %s" % (kind, url))
    return found


def _render(template, **context):
    from flask import render_template, session

    app = create_app()
    with app.test_request_context("/"):
        session["role"] = "instructor"
        return render_template(template, **context)


class TestLocalTailwind(unittest.TestCase):
    def _pages(self):
        empty_report = build_revision_report([], date(2026, 10, 20))
        return {
            "login": _render("login.html"),
            "signup": _render("signup.html"),
            "class_detail": _render(
                "class_detail.html",
                class_id="c1",
                class_name="Test Upload",
                class_days="MW",
                class_section="F26",
                students=[],
                all_students=[],
                learning_objectives=[],
                assignments=[],
                revision_report=empty_report,
                revision_printed_on="2026-10-20",
            ),
            "reports": _render(
                "class_reports.html",
                class_id="c1",
                class_name="Test Upload",
                class_days="MW",
                class_section="F26",
                students=[],
                learning_objectives=[],
                assignments=[],
                instructor_title="Professor Estes",
            ),
        }

    def test_pages_load_no_foreign_script_or_stylesheet(self):
        for name, html in self._pages().items():
            self.assertEqual(foreign_asset_urls(html), [], name)

    def test_built_css_is_served(self):
        app = create_app()
        response = app.test_client().get("/static/css/tailwind.css")
        self.assertEqual(response.status_code, 200)
        self.assertIn("css", response.content_type)

    def test_built_css_contains_dynamic_utilities(self):
        self.assertTrue(CSS_PATH.is_file(), "app/static/css/tailwind.css is missing")
        css = CSS_PATH.read_text(encoding="utf-8")
        missing = [token for token in DYNAMIC_UTILITIES if _selector(token) not in css]
        self.assertEqual(missing, [])

    def test_mastery_rules_are_in_speed_grader_and_reports(self):
        pages = {
            "speed_grader": _render(
                "class_speed_grader.html",
                class_id="c1",
                instructor_mode="mark",
                assignments=[],
                students=[],
                lo_names=[],
                auto_convert_m=False,
            ),
            "reports": _render(
                "class_reports.html",
                class_id="c1",
                class_name="Test Upload",
                class_days="MW",
                class_section="F26",
                students=[],
                learning_objectives=[],
                assignments=[],
                instructor_title="Professor Estes",
            ),
        }
        for name, html in pages.items():
            for rule in MASTERY_RULES:
                self.assertRegex(html, r"\.%s(?:\s|,|\{)" % rule, "%s %s" % (name, rule))

    def test_vendor_javascript_is_not_scanned(self):
        self.assertTrue(CSS_PATH.is_file(), "app/static/css/tailwind.css is missing")
        config = (ROOT / "tailwind.config.js").read_text(encoding="utf-8")
        self.assertIn("!app/static/js/vendor/**", config)
        css = CSS_PATH.read_text(encoding="utf-8")
        app_tokens = _source_tokens(exclude_vendor=True)
        vendor_only = _source_tokens(vendor_only=True) - app_tokens
        leaked = sorted(
            token
            for token in vendor_only
            if ("-" in token or ":" in token or token in _SINGLE_WORD_UTILITIES)
            and (
                _selector(token) + "{" in css
                or _selector(token) + "," in css
            )
        )
        self.assertEqual(leaked, [])


_TOKEN = re.compile(
    r"^(?:[a-z0-9-]+:)*[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:/[0-9]+)?$"
)
_STRINGS = re.compile(r"""(['"`])(?:\\.|(?!\1).)*?\1""", re.S)


def _source_tokens(exclude_vendor=False, vendor_only=False):
    tokens = set()
    if vendor_only:
        roots = [VENDOR_DIR]
    else:
        roots = [ROOT / "app" / "templates", ROOT / "app" / "static" / "js"]
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix.lower() not in {".html", ".js"}:
                continue
            if exclude_vendor and (path == VENDOR_DIR or VENDOR_DIR in path.parents):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for match in _STRINGS.finditer(text):
                body = match.group(0)[1:-1]
                for piece in body.split():
                    if _TOKEN.match(piece):
                        tokens.add(piece)
    return tokens


if __name__ == "__main__":
    unittest.main()
