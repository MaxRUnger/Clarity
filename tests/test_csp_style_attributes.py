"""Style attributes are gone. Width and column count are set from data attributes."""

import re
import unittest
from pathlib import Path

from flask import session

from app import create_app

ROOT = Path(__file__).resolve().parents[1]

STYLE_ATTR = re.compile(r"""style=["']|setAttribute\(\s*['"]style['"]""")
TEMPLATE_STYLE = re.compile(r"style\s*=")

BAR_CLASS = "h-full bg-green-500 rounded-full progress-bar-fill"


class _Objective:
    def __init__(self):
        self.is_passed = True
        self.vendor_code = "D1"
        self.name = "Derivatives"
        self.m_count = 2
        self.required_ms = 2
        self.mr_count = 0
        self.grades_list = ["M"]
        self.grades_meta = [{"counts_for_mastery": True}]


class _RosterStudent:
    def __init__(self, total, passed_count):
        self.id = "stu-1"
        self.name = "Ada"
        self.email = "ada@example.edu"
        self.objective_total = total
        self.learning_objectives = [_Objective() for _ in range(passed_count)]


def _render(template, **kwargs):
    app = create_app()
    with app.test_request_context():
        session["user_id"] = "inst-1"
        session["role"] = "instructor"
        from flask import render_template

        return render_template(template, **kwargs)


class TestCspStyleAttributes(unittest.TestCase):
    def test_templates_have_no_style_attributes(self):
        self.assertIsNotNone(STYLE_ATTR.search('style="width:1px"'))
        self.assertIsNone(STYLE_ATTR.search("el.style.width = '1px'"))
        files = sorted((ROOT / "app/templates").rglob("*.html"))
        self.assertGreaterEqual(len(files), 21)
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(TEMPLATE_STYLE.search(text), path.name)

    def test_page_scripts_have_no_style_attributes(self):
        files = sorted(path for path in (ROOT / "app/static/js").glob("*.js"))
        self.assertGreaterEqual(len(files), 19)
        self.assertTrue((ROOT / "app/static/js/vendor").is_dir())
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(STYLE_ATTR.search(text), path.name)

    def test_app_css_has_panel_and_fill_rules(self):
        path = ROOT / "app/static/css/app.css"
        self.assertTrue(path.is_file(), "app/static/css/app.css is missing")
        text = path.read_text(encoding="utf-8")
        self.assertIn(".assignments-list-panel", text)
        self.assertIn(".assignments-list-scroll", text)
        self.assertIn(".assignments-objective-panel", text)
        self.assertIn("min-height: 600px;", text)
        self.assertIn("max-height: 560px;", text)
        fill_at = text.find(".progress-bar-fill")
        panels_at = text.find(".assignments-objective-panel")
        self.assertGreater(panels_at, -1)
        self.assertGreater(fill_at, -1)
        self.assertLess(panels_at, fill_at)
        self.assertTrue(
            ".progress-bar-fill { width: 0; }" in text,
            "app.css is missing .progress-bar-fill { width: 0; }",
        )

    def test_app_css_link_follows_tailwind(self):
        text = (ROOT / "app/templates/app_base.html").read_text(encoding="utf-8")
        tailwind = text.find("css/tailwind.css")
        app_css = text.find("css/app.css")
        self.assertGreater(tailwind, -1)
        self.assertGreater(app_css, -1)
        self.assertLess(tailwind, app_css)

    def test_progress_bar_uses_data_attribute_and_fill_class(self):
        text = (ROOT / "app/templates/_components.html").read_text(encoding="utf-8")
        start = text.find("{% macro progress_bar")
        end = text.find("{% endmacro %}", start)
        macro = text[start:end]
        self.assertGreater(start, -1)
        self.assertGreater(end, -1)
        expected = '<div class="%s" data-progress="{{ pct }}"></div>' % BAR_CLASS
        self.assertTrue(expected in macro, "progress bar is missing the fill class and data-progress")
        self.assertNotIn("transition-all", macro)
        self.assertNotIn("|safe", macro)

    def test_history_table_uses_data_count(self):
        text = (ROOT / "app/templates/classes/student_history_table.html").read_text(encoding="utf-8")
        self.assertTrue(
            'data-history-lo-count="{{ learning_objectives|length or 1 }}"' in text,
            "history table is missing data-history-lo-count",
        )
        self.assertIsNone(TEMPLATE_STYLE.search(text), "student_history_table.html")

    def test_speed_grader_empty_background_is_a_rule(self):
        text = (ROOT / "app/templates/classes/class_speed_grader.html").read_text(encoding="utf-8")
        normalized = text.replace("\r\n", "\n")
        self.assertTrue(
            ".hw-pct-input { background: #64748b; }" in normalized,
            "speed grader is missing the empty-background rule",
        )
        self.assertTrue(
            'style="background:#64748b"' not in text,
            "speed grader still has a style attribute",
        )

    def test_app_shell_sets_width_and_count_before_print_timer(self):
        text = (ROOT / "app/static/js/app_shell.js").read_text(encoding="utf-8")
        width_at = text.find("bars[i].style.width = pct + '%'")
        count_at = text.find("tables[j].style.setProperty('--history-lo-count', count)")
        timer_at = text.find("}, 250);")
        self.assertGreater(width_at, -1)
        self.assertGreater(count_at, -1)
        self.assertGreater(timer_at, -1)
        self.assertLess(width_at, count_at)
        self.assertLess(count_at, timer_at)

    def test_app_shell_is_not_referenced_at_v3(self):
        text = (ROOT / "app/templates/app_base.html").read_text(encoding="utf-8")
        self.assertTrue(
            "js/app_shell.js') }}?v=3" not in text,
            "app_shell.js is still referenced at ?v=3",
        )
        self.assertTrue(
            "js/app_shell.js') }}?v=4" in text,
            "app_shell.js is not referenced at ?v=4",
        )

    def test_stale_asset_versions_were_bumped(self):
        expected = {
            "app/templates/app_base.html": (
                "js/student_names.js') }}?v=3",
                "js/ui_helpers.js') }}?v=5",
                "css/tailwind.css') }}?v=2",
                "js/session_timeout.js') }}?v=2",
            ),
            "app/templates/auth_base.html": (
                "css/tailwind.css') }}?v=2",
                "js/session_timeout.js') }}?v=2",
            ),
            "app/templates/classes/class_speed_grader.html": (
                "js/class_speed_grader.js') }}?v=2",
            ),
            "app/templates/classes/class_reports.html": (
                "js/class_reports.js') }}?v=2",
            ),
            "app/templates/classes/update_grade.html": (
                "js/csv_wizard.js') }}?v=23",
            ),
        }
        for rel, needles in expected.items():
            text = (ROOT / rel).read_text(encoding="utf-8")
            for needle in needles:
                self.assertTrue(needle in text, "%s is missing %s" % (rel, needle))

    def test_students_page_zero_progress_has_data_attribute(self):
        html = _render(
            "classes/class_students.html",
            class_id="class-1",
            class_name="Algebra",
            class_days="MWF",
            class_section="01",
            students=[_RosterStudent(0, 0)],
        )
        self.assertTrue('data-progress="0"' in html, "zero progress is missing data-progress")
        self.assertTrue('class="%s"' % BAR_CLASS in html, "zero progress is missing progress-bar-fill")
        self.assertIsNone(TEMPLATE_STYLE.search(html), "students zero")

    def test_students_page_full_progress_has_data_attribute(self):
        html = _render(
            "classes/class_students.html",
            class_id="class-1",
            class_name="Algebra",
            class_days="MWF",
            class_section="01",
            students=[_RosterStudent(5, 5)],
        )
        self.assertTrue('data-progress="100"' in html, "full progress is missing data-progress")
        self.assertTrue('class="%s"' % BAR_CLASS in html, "full progress is missing progress-bar-fill")
        self.assertIsNone(TEMPLATE_STYLE.search(html), "students full")

    def test_history_page_empty_objectives_uses_floor(self):
        html = _render(
            "classes/student_history.html",
            class_id="class-1",
            class_name="Algebra",
            class_days="MWF",
            class_section="01",
            student_id="stu-1",
            student_name="Ada",
            student_email="ada@example.edu",
            instructor_title="Instructor",
            learning_objectives=[],
            history_assignments=[],
        )
        self.assertTrue('data-history-lo-count="1"' in html, "empty objectives did not render count 1")
        self.assertIsNone(TEMPLATE_STYLE.search(html), "history empty")

    def test_history_page_seven_objectives(self):
        objectives = [{"vendor_code": "D%s" % index} for index in range(1, 8)]
        html = _render(
            "classes/student_history.html",
            class_id="class-1",
            class_name="Algebra",
            class_days="MWF",
            class_section="01",
            student_id="stu-1",
            student_name="Ada",
            student_email="ada@example.edu",
            instructor_title="Instructor",
            learning_objectives=objectives,
            history_assignments=[],
        )
        self.assertTrue('data-history-lo-count="7"' in html, "seven objectives did not render count 7")
        self.assertIsNone(TEMPLATE_STYLE.search(html), "history seven")
