"""Style attributes are gone. Width and column count are set from data attributes."""

import re
import unittest
from pathlib import Path

from flask import session

from app import create_app

ROOT = Path(__file__).resolve().parents[1]

STYLE_ATTR = re.compile(r"""style=["']|setAttribute\(\s*['"]style['"]""")
TEMPLATE_STYLE = re.compile(r"style\s*=")
STYLE_ELEMENT = re.compile(r"<style")
CREATE_STYLE = re.compile(r"""createElement\(\s*['"]style['"]\s*\)""")

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
        self.assertIsNotNone(STYLE_ELEMENT.search("<style>a{}</style>"))
        self.assertIsNone(STYLE_ELEMENT.search("el.style.width = '1px'"))
        files = sorted((ROOT / "app/templates").rglob("*.html"))
        self.assertGreaterEqual(len(files), 21)
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(TEMPLATE_STYLE.search(text), path.name)
            self.assertIsNone(STYLE_ELEMENT.search(text), path.name)
        moved = {
            "app/templates/classes/class_students.html": "css/student_progress_print.css') }}?v=1",
            "app/templates/classes/class_student_detail.html": "css/student_progress_print.css') }}?v=1",
            "app/templates/classes/class_detail.html": "css/class_detail_print.css') }}?v=1",
            "app/templates/classes/student_history.html": "css/student_history_sheet_print.css') }}?v=1",
            "app/templates/classes/student_history_print.html": "css/student_history_batch_print.css') }}?v=1",
            "app/templates/classes/class_reports.html": "css/class_reports_print.css') }}?v=1",
            "app/templates/classes/class_learning_objectives_summary.html": "css/learning_objectives_print.css') }}?v=1",
        }
        for rel, needle in moved.items():
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertGreater(text.find(needle), -1, rel)

    def test_page_scripts_have_no_style_attributes(self):
        self.assertIsNotNone(CREATE_STYLE.search("document.createElement('style')"))
        self.assertIsNotNone(CREATE_STYLE.search('document.createElement("style")'))
        self.assertIsNone(CREATE_STYLE.search("el.style.width = '1px'"))
        files = sorted(path for path in (ROOT / "app/static/js").glob("*.js"))
        self.assertGreaterEqual(len(files), 19)
        self.assertTrue((ROOT / "app/static/js/vendor").is_dir())
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(STYLE_ATTR.search(text), path.name)
            self.assertIsNone(STYLE_ELEMENT.search(text), path.name)
            self.assertIsNone(CREATE_STYLE.search(text), path.name)

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
        css = (ROOT / "app/static/css/app.css").read_text(encoding="utf-8")
        rule = ".hw-pct-input { background: #64748b; }"
        self.assertGreater(css.find(rule), -1)
        self.assertNotIn(rule, text.replace("\r\n", "\n"))
        self.assertTrue(
            'style="background:#64748b"' not in text,
            "speed grader still has a style attribute",
        )

    def test_shared_mastery_colors_are_in_app_css(self):
        css = (ROOT / "app/static/css/app.css").read_text(encoding="utf-8")
        rule = ".mastery-q  { background: #fde047; color: #713f12; border-color: #a16207; }"
        self.assertGreater(css.find(rule), -1)
        components = (ROOT / "app/templates/_components.html").read_text(encoding="utf-8")
        self.assertNotIn("{% macro mastery_badge_styles() %}", components)

    def test_speed_grader_screen_block_moved(self):
        css = (ROOT / "app/static/css/app.css").read_text(encoding="utf-8")
        page = (ROOT / "app/templates/classes/class_speed_grader.html").read_text(encoding="utf-8")
        rule = "#graderTable .grade-cell .mastery-badge {"
        self.assertGreater(css.find(rule), -1)
        self.assertGreater(css.find("#graderTableScrollWrap {"), -1)
        self.assertNotIn("<style>", page.replace("\r\n", "\n"))

    def test_signup_animation_moved(self):
        css = (ROOT / "app/static/css/app.css").read_text(encoding="utf-8")
        page = (ROOT / "app/templates/auth/signup.html").read_text(encoding="utf-8")
        self.assertGreater(css.find("animation: slideUp 0.25s ease-out;"), -1)
        self.assertNotIn("@keyframes slideUp", page)

    def test_detail_scrollbar_moved(self):
        css = (ROOT / "app/static/css/app.css").read_text(encoding="utf-8")
        page = (ROOT / "app/templates/classes/class_detail.html").read_text(encoding="utf-8")
        rule = "#studentProgressCard .student-progress-scroll {"
        self.assertGreater(css.find(rule), -1)
        self.assertNotIn(rule, page)
        print_path = ROOT / "app" / "static" / "css" / "class_detail_print.css"
        print_css = print_path.read_text(encoding="utf-8") if print_path.is_file() else ""
        chip = print_css.find(".revision-grade-q {")
        self.assertGreater(chip, -1)
        self.assertNotIn(".revision-grade-q {", page)

    def test_wizard_screen_block_moved(self):
        css = (ROOT / "app/static/css/app.css").read_text(encoding="utf-8")
        page = (ROOT / "app/templates/classes/update_grade.html").read_text(encoding="utf-8")
        rule = "#extractedStudentsTable .grade-cell {"
        self.assertGreater(css.find(rule), -1)
        self.assertNotIn("<style>", page)

    def test_reports_screen_colors_moved_and_footprint_stays(self):
        css = (ROOT / "app/static/css/app.css").read_text(encoding="utf-8")
        page = (ROOT / "app/templates/classes/class_reports.html").read_text(encoding="utf-8")
        self.assertGreater(css.find(".mastery-m  { background: #dcfce7; color: #15803d; border-color: #86efac; }"), -1)
        self.assertNotIn(".mastery-m  { background: #dcfce7;", page)
        reports_path = ROOT / "app" / "static" / "css" / "class_reports_print.css"
        reports = reports_path.read_text(encoding="utf-8") if reports_path.is_file() else ""
        footprint = reports.find("td[data-lo-id] .mastery-badge {")
        self.assertGreater(footprint, -1)
        self.assertNotIn("td[data-lo-id] .mastery-badge {", page)
        self.assertNotIn("#assignmentReport td[data-lo-id] .mastery-badge", css)
        scale = reports.find("--print-scale: 1;")
        self.assertGreater(scale, -1)
        self.assertGreater(scale, footprint)

    def test_app_css_v2_follows_tailwind(self):
        for rel in ("app/templates/app_base.html", "app/templates/auth_base.html"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            tailwind = text.find("css/tailwind.css")
            app_css = text.find("css/app.css') }}?v=2")
            self.assertGreater(tailwind, -1)
            self.assertGreater(app_css, -1)
            self.assertLess(tailwind, app_css)

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
                "js/class_speed_grader.js') }}?v=9",
            ),
            "app/templates/classes/class_reports.html": (
                "js/class_reports.js') }}?v=3",
            ),
            "app/templates/classes/update_grade.html": (
                "js/csv_wizard.js') }}?v=24",
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

    def test_progress_print_rules_follow_app_css(self):
        css_path = ROOT / "app" / "static" / "css" / "student_progress_print.css"
        css = css_path.read_text(encoding="utf-8") if css_path.is_file() else ""
        rule = css.find(".lo-row-rule { display: none; }")
        page = css.find("@page { size: letter portrait; margin: 0; }")
        breaks = css.find("  .roster-print-student {")
        last = css.find(".roster-print-student:last-child {")
        self.assertGreater(rule, -1)
        self.assertGreater(page, -1)
        self.assertGreater(breaks, -1)
        self.assertGreater(last, -1)
        self.assertLess(rule, page)
        self.assertLess(page, breaks)
        self.assertLess(breaks, last)
        self.assertEqual(css.count("@page"), 1)
        students = _render(
            "classes/class_students.html",
            class_id="class-1",
            class_name="Algebra",
            class_days="MWF",
            class_section="01",
            students=[],
        )
        detail = _render(
            "classes/class_student_detail.html",
            class_id="class-1",
            class_name="Algebra",
            class_days="MWF",
            class_section="01",
            student={
                "id": "stu-1",
                "name": "Ada",
                "email": "ada@example.edu",
                "objective_total": 0,
                "learning_objectives": [],
            },
        )
        link = "css/student_progress_print.css?v=1"
        for html in (students, detail):
            tailwind = html.find("css/tailwind.css")
            app_css = html.find("css/app.css")
            print_css = html.find(link)
            self.assertGreater(tailwind, -1)
            self.assertGreater(app_css, -1)
            self.assertGreater(print_css, -1)
            self.assertLess(tailwind, app_css)
            self.assertLess(app_css, print_css)
            self.assertNotIn("<style", html)
        components = (ROOT / "app" / "templates" / "_components.html").read_text(encoding="utf-8")
        self.assertNotIn("student_progress_print_styles", components)

    def test_detail_print_page_follows_app_css(self):
        from datetime import date
        from app.helpers.revision_report import build_revision_report
        css_path = ROOT / "app" / "static" / "css" / "class_detail_print.css"
        css = css_path.read_text(encoding="utf-8") if css_path.is_file() else ""
        page_at = css.find("@page { size: letter portrait; margin: 0; }")
        self.assertGreater(page_at, -1)
        self.assertEqual(css.count("@page"), 1)
        html = _render(
            "classes/class_detail.html",
            class_id="c1",
            class_name="Algebra",
            class_days="MW",
            class_section="01",
            students=[],
            all_students=[],
            learning_objectives=[],
            assignments=[],
            revision_report=build_revision_report([], date(2026, 10, 20)),
            revision_printed_on="2026-10-20",
        )
        tailwind = html.find("css/tailwind.css")
        app_css = html.find("css/app.css")
        print_css = html.find("css/class_detail_print.css?v=1")
        self.assertGreater(tailwind, -1)
        self.assertGreater(app_css, -1)
        self.assertGreater(print_css, -1)
        self.assertLess(tailwind, app_css)
        self.assertLess(app_css, print_css)
        self.assertNotIn("<style", html)

    def test_history_sheet_print_follows_table_css(self):
        css_path = ROOT / "app" / "static" / "css" / "student_history_sheet_print.css"
        css = css_path.read_text(encoding="utf-8") if css_path.is_file() else ""
        page_at = css.find("@page { size: letter portrait; margin: 0.22in; }")
        self.assertGreater(page_at, -1)
        self.assertEqual(css.count("@page"), 1)
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
        tailwind = html.find("css/tailwind.css")
        app_css = html.find("css/app.css")
        table_css = html.find("css/student_history_table.css")
        sheet_css = html.find("css/student_history_sheet_print.css?v=1")
        self.assertGreater(tailwind, -1)
        self.assertGreater(app_css, -1)
        self.assertGreater(table_css, -1)
        self.assertGreater(sheet_css, -1)
        self.assertLess(tailwind, app_css)
        self.assertLess(app_css, table_css)
        self.assertLess(table_css, sheet_css)
        self.assertNotIn("<style", html)
