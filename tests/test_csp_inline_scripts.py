import re
import unittest
from pathlib import Path

from flask import session

from app import create_app

ROOT = Path(__file__).resolve().parents[1]

TEMPLATES = (
    "app/templates/auth/login.html",
    "app/templates/auth/signup.html",
    "app/templates/auth/forgot_password.html",
    "app/templates/auth/reset_password.html",
    "app/templates/classes/instructor_select_class.html",
    "app/templates/classes/archived_classes.html",
    "app/templates/classes/class_students.html",
    "app/templates/classes/class_student_detail.html",
    "app/templates/classes/student_history.html",
    "app/templates/classes/student_history_print.html",
    "app/templates/classes/class_learning_objectives_summary.html",
    "app/templates/_components.html",
    "app/templates/classes/update_grade.html",
)

ON_ATTR = re.compile(r"\son[a-zA-Z]+\s*=")
SCRIPT_OPEN = re.compile(r"<script\b([^>]*)>", re.I)
JS_HANDLER = re.compile(r"""\bon[a-z]+\s*=\s*\\?["']|setAttribute\(\s*['"]on""")
JS_EVAL = re.compile(r"\beval\s*\(|\bnew\s+Function\s*\(")
SRC_ATTR = re.compile(r"\ssrc\s*=")


def _script_opens(html):
    return [match.group(0) for match in SCRIPT_OPEN.finditer(html)]


class _Student:
    def __init__(self):
        self.id = "stu-1"
        self.name = "Ada"
        self.email = "ada@example.edu"
        self.learning_objectives = []
        self.objective_total = 0


class TestCspInlineScripts(unittest.TestCase):
    def test_templates_have_no_inline_handlers_or_executable_scripts(self):
        for rel in TEMPLATES:
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIsNone(ON_ATTR.search(text), rel)
            for match in SCRIPT_OPEN.finditer(text):
                attrs = match.group(1)
                if "src=" in attrs:
                    continue
                self.assertIn("application/json", attrs, rel)

    def test_csv_wizard_has_no_generated_handlers_and_has_delegated_listeners(self):
        text = (ROOT / "app/static/js/csv_wizard.js").read_text(encoding="utf-8")
        self.assertNotIn("onclick=", text)
        self.assertNotIn("onchange=", text)
        self.assertNotIn("oninput=", text)
        self.assertIn("closest('[data-set-all-extra]')", text)
        self.assertIn("classList.contains('extra-lo-toggle')", text)
        self.assertIn("classList.contains('name-input')", text)
        self.assertIn("classList.contains('grade-input-hw')", text)

    def test_auth_pages_render_page_script_after_theme(self):
        app = create_app()
        client = app.test_client()
        pages = (
            ("/login", "js/login.js"),
            ("/signup", "js/signup.js"),
            ("/forgot-password", "js/forgot_password.js"),
            ("/reset-password", "js/reset_password.js"),
        )
        for path, name in pages:
            response = client.get(path)
            self.assertEqual(response.status_code, 200, path)
            html = response.get_data(as_text=True)
            csrf = html.find("js/auth_csrf.js")
            theme = html.find("js/auth_theme.js")
            page = html.find(name)
            self.assertGreaterEqual(csrf, 0, path)
            self.assertGreater(theme, csrf, path)
            self.assertGreater(page, theme, path)
            self.assertNotRegex(html, ON_ATTR)

    def test_class_pages_render_script_tags_in_document_order(self):
        from flask import render_template

        app = create_app()
        student = _Student()
        with app.test_request_context():
            session["user_id"] = "inst-1"
            session["role"] = "instructor"
            pages = {
                "instructor": render_template(
                    "classes/instructor_select_class.html",
                    classes=[],
                    create_class_token="create-token",
                    copy_class_token="copy-token",
                    class_days=["MWF"],
                ),
                "archived": render_template(
                    "classes/archived_classes.html",
                    archives=[],
                ),
                "students": render_template(
                    "classes/class_students.html",
                    class_id="class-1",
                    class_name="Algebra",
                    class_days="MWF",
                    class_section="01",
                    students=[],
                ),
                "detail": render_template(
                    "classes/class_student_detail.html",
                    class_id="class-1",
                    class_name="Algebra",
                    class_days="MWF",
                    class_section="01",
                    student=student,
                ),
                "history": render_template(
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
                ),
                "history_print": render_template(
                    "classes/student_history_print.html",
                    class_id="class-1",
                    class_name="Algebra",
                    sheets=[],
                ),
                "objectives": render_template(
                    "classes/class_learning_objectives_summary.html",
                    class_id="class-1",
                    class_name="Algebra",
                    class_days="MWF",
                    class_section="01",
                    printed_on="2026-11-05",
                    learning_objectives=[],
                    total_students=0,
                    grid_rows=[],
                ),
                "upload": render_template(
                    "classes/update_grade.html",
                    class_id="class-1",
                    assignments=[{"id": "asg-1", "name": "Quiz"}],
                    roster=[],
                ),
            }
        instructor = pages["instructor"]
        instructor_mode_at = instructor.find('id="instructorModeJson"')
        instructor_js_at = instructor.find("js/instructor_select_class.js")
        instructor_theme_at = instructor.find("js/auth_theme.js")
        self.assertGreater(instructor_mode_at, -1)
        self.assertLess(instructor_mode_at, instructor_js_at)
        self.assertLess(instructor_js_at, instructor_theme_at)
        archived = pages["archived"]
        archived_theme_at = archived.find("js/auth_theme.js")
        self.assertGreater(archived_theme_at, -1)
        self.assertLess(archived_theme_at, archived.find("js/archived_classes.js"))
        students = pages["students"]
        students_js_at = students.find("js/class_students.js")
        progress_css_at = students.find("css/student_progress_print.css?v=1")
        self.assertGreater(students_js_at, -1)
        self.assertGreater(progress_css_at, -1)
        self.assertLess(students_js_at, progress_css_at)
        self.assertNotIn("<style", students)
        self.assertLess(students_js_at, students.find("js/app_shell.js"))
        self.assertIn("data-print", students)
        self.assertNotIn('id="printStudentSheetsBtn"', students)
        self.assertIn("js/class_students.js?v=2", students)
        detail = pages["detail"]
        self.assertNotIn("js/class_student_detail.js", detail)
        self.assertIn("data-print", detail)
        self.assertIn("js/app_shell.js?v=4", detail)
        history = pages["history"]
        history_payload_at = history.find('id="studentHistoryEmailPayload"')
        history_js_at = history.find("js/student_history.js")
        self.assertGreater(history_payload_at, -1)
        self.assertLess(history_payload_at, history_js_at)
        self.assertLess(history_js_at, history.find("js/app_shell.js"))
        self.assertIn("data-print", history)
        self.assertNotIn('id="printStudentHistoryBtn"', history)
        self.assertIn("js/student_history.js?v=2", history)
        printed = pages["history_print"]
        self.assertNotIn("js/student_history_print.js", printed)
        history_auto_at = printed.find("data-auto-print")
        batch_css_at = printed.find("css/student_history_batch_print.css?v=1")
        table_css_at = printed.find("css/student_history_table.css")
        tailwind_at = printed.find("css/tailwind.css")
        app_css_at = printed.find("css/app.css")
        self.assertGreater(history_auto_at, -1)
        self.assertGreater(batch_css_at, -1)
        self.assertGreater(table_css_at, -1)
        self.assertGreater(tailwind_at, -1)
        self.assertGreater(app_css_at, -1)
        self.assertLess(tailwind_at, app_css_at)
        self.assertLess(app_css_at, table_css_at)
        self.assertLess(table_css_at, batch_css_at)
        self.assertLess(history_auto_at, batch_css_at)
        self.assertNotIn("<style", printed)
        objectives = pages["objectives"]
        self.assertNotIn("js/class_learning_objectives_summary.js", objectives)
        self.assertIn("data-print", objectives)
        summary_auto_at = objectives.find("data-auto-print")
        summary_css_at = objectives.find("css/learning_objectives_print.css?v=1")
        summary_tailwind_at = objectives.find("css/tailwind.css")
        summary_app_at = objectives.find("css/app.css")
        self.assertGreater(summary_auto_at, -1)
        self.assertGreater(summary_css_at, -1)
        self.assertGreater(summary_tailwind_at, -1)
        self.assertGreater(summary_app_at, -1)
        self.assertLess(summary_tailwind_at, summary_app_at)
        self.assertLess(summary_app_at, summary_css_at)
        self.assertLess(summary_auto_at, summary_css_at)
        self.assertNotIn("<style", objectives)
        upload = pages["upload"]
        roster_at = upload.find('id="classRosterJson"')
        class_at = upload.find('id="classIdJson"')
        assignments_at = upload.find('id="assignmentsJson"')
        wizard_at = upload.find("js/csv_wizard.js")
        shell_at = upload.find("js/app_shell.js")
        self.assertGreater(roster_at, -1)
        self.assertLess(roster_at, class_at)
        self.assertLess(class_at, assignments_at)
        self.assertGreater(assignments_at, -1)
        self.assertLess(assignments_at, wizard_at)
        self.assertLess(wizard_at, shell_at)
        self.assertIn("?v=24", upload[wizard_at:wizard_at + 80])
        self.assertNotIn("js/update_grade.js", upload)
        wizard_src = (ROOT / "app/static/js/csv_wizard.js").read_text(encoding="utf-8")
        wizard_class_at = wizard_src.find("window.CLASS_ID")
        wizard_selected_at = wizard_src.find("function onAssignmentSelected")
        self.assertGreater(wizard_class_at, -1)
        self.assertLess(wizard_class_at, wizard_selected_at)
        self.assertIn("window.ASSIGNMENTS_DATA", wizard_src)
        self.assertIn("getElementById('csvInput')", wizard_src)
        self.assertIn("getElementById('confirmBackBtn')", wizard_src)
        shell_src = (ROOT / "app/static/js/app_shell.js").read_text(encoding="utf-8")
        shell_click_at = shell_src.find("closest('[data-print]')")
        shell_print_at = shell_src.find("window.print()", shell_click_at)
        shell_auto_at = shell_src.find("querySelector('[data-auto-print]')")
        shell_gate_at = shell_src.find('params.get("print") !== "1"', shell_auto_at)
        shell_load_at = shell_src.find('addEventListener("load"', shell_gate_at)
        shell_timer_at = shell_src.find("setTimeout", shell_load_at)
        shell_auto_print_at = shell_src.find("window.print()", shell_timer_at)
        shell_delay_at = shell_src.find("}, 250);", shell_timer_at)
        shell_replaced_at = shell_src.find("history.replaceState", shell_auto_print_at)
        shell_mode_at = shell_src.find("getElementById('instructorModeJson')", shell_replaced_at)
        self.assertGreater(shell_click_at, -1)
        self.assertGreater(shell_print_at, shell_click_at)
        self.assertGreater(shell_auto_at, shell_print_at)
        self.assertGreater(shell_gate_at, shell_auto_at)
        self.assertGreater(shell_load_at, shell_gate_at)
        self.assertGreater(shell_timer_at, shell_load_at)
        self.assertGreater(shell_auto_print_at, shell_timer_at)
        self.assertGreater(shell_delay_at, shell_auto_print_at)
        self.assertGreater(shell_replaced_at, shell_auto_print_at)
        self.assertGreater(shell_mode_at, shell_replaced_at)
        self.assertIn("applyInstructorModeVisibility", shell_src[shell_mode_at:])
        self.assertIn("copyEmailFromBtn(btn)", shell_src)
        self.assertNotIn("btn.onclick", shell_src)
        for name, html in pages.items():
            self.assertIsNone(ON_ATTR.search(html), name)
            for opener in _script_opens(html):
                if "src=" in opener:
                    continue
                self.assertIn("application/json", opener, name)

    def test_assignments_page_has_no_inline_script_and_keeps_script_order(self):
        from flask import render_template
        text = (ROOT / "app/templates/classes/class_assignments.html").read_text(encoding="utf-8")
        self.assertIsNone(ON_ATTR.search(text))
        for match in SCRIPT_OPEN.finditer(text):
            attrs = match.group(1)
            if "src=" in attrs:
                continue
            self.assertIn("application/json", attrs)
        app = create_app()
        with app.test_request_context():
            session["user_id"] = "inst-1"
            session["role"] = "instructor"
            html = render_template(
                "classes/class_assignments.html",
                class_id="class-1",
                class_name="Algebra",
                assignments=[{
                    "id": "asg-1",
                    "name": "Quiz",
                    "assignment_type": "exam",
                    "homework_group": "HW",
                    "assignment_objectives": [],
                }],
                all_los=[],
                unattached_count=0,
            )
        class_json = html.find('id="classIdJson"')
        config = html.find('id="loCsvConfig"')
        mode_json = html.find('id="instructorModeJson"')
        page_js = html.find("js/class_assignments.js?v=2")
        shell = html.find("js/app_shell.js")
        self.assertGreater(class_json, -1)
        self.assertLess(class_json, config)
        self.assertLess(config, mode_json)
        self.assertLess(mode_json, page_js)
        self.assertLess(page_js, shell)
        self.assertEqual(html.count("js/class_assignments.js"), 1)
        self.assertNotIn("js/class_assignments_lo_csv.js", html)
        self.assertNotIn("js/class_assignments_mode.js", html)

    def test_class_detail_page_has_no_inline_script_and_keeps_script_order(self):
        from datetime import date
        from flask import render_template
        from app.helpers.revision_report import build_revision_report
        text = (ROOT / "app/templates/classes/class_detail.html").read_text(encoding="utf-8")
        self.assertIsNone(ON_ATTR.search(text))
        for match in SCRIPT_OPEN.finditer(text):
            attrs = match.group(1)
            if "src=" in attrs:
                continue
            self.assertIn("application/json", attrs)
        app = create_app()
        with app.test_request_context():
            session["user_id"] = "inst-1"
            session["role"] = "instructor"
            html = render_template(
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
        class_json = html.find('id="classIdJson"')
        mode_json = html.find('id="instructorModeJson"')
        page_js = html.find("js/class_detail.js?v=2")
        shell = html.find("js/app_shell.js?v=4")
        self.assertGreater(class_json, -1)
        self.assertLess(class_json, mode_json)
        self.assertLess(mode_json, page_js)
        self.assertLess(page_js, shell)
        self.assertIn("data-print", html)
        self.assertIn('id="progressAssignmentsJson"', html)
        self.assertIn('id="progressStudentsJson"', html)
        self.assertIn('id="dashboardAllStudentsJson"', html)
        self.assertIsNone(ON_ATTR.search(html))

    def test_speed_grader_page_has_no_inline_script_and_keeps_script_order(self):
        from flask import render_template
        text = (ROOT / "app/templates/classes/class_speed_grader.html").read_text(encoding="utf-8")
        self.assertIsNone(ON_ATTR.search(text))
        for match in SCRIPT_OPEN.finditer(text):
            attrs = match.group(1)
            if "src=" in attrs:
                continue
            self.assertIn("application/json", attrs)
        app = create_app()
        with app.test_request_context():
            session["user_id"] = "inst-1"
            session["role"] = "instructor"
            html = render_template(
                "classes/class_speed_grader.html",
                class_id="c1",
                instructor_mode="mark",
                assignments=[],
                students=[],
                lo_names=[],
                auto_convert_m=False,
            )
        assignment_json = html.find('id="assignmentData"')
        mode_json = html.find('id="instructorModeJson"')
        class_json = html.find('id="classIdJson"')
        auto_json = html.find('id="autoConvertMJson"')
        page_js = html.find("js/class_speed_grader.js?v=6")
        shell = html.find("js/app_shell.js?v=4")
        self.assertGreater(assignment_json, -1)
        self.assertLess(assignment_json, mode_json)
        self.assertLess(mode_json, class_json)
        self.assertLess(class_json, auto_json)
        self.assertLess(auto_json, page_js)
        self.assertLess(page_js, shell)
        self.assertIsNone(ON_ATTR.search(html))

    def test_reports_page_has_no_inline_script_and_keeps_script_order(self):
        from flask import render_template
        text = (ROOT / "app/templates/classes/class_reports.html").read_text(encoding="utf-8")
        self.assertIsNone(ON_ATTR.search(text))
        for match in SCRIPT_OPEN.finditer(text):
            attrs = match.group(1)
            if "src=" in attrs:
                continue
            self.assertIn("application/json", attrs)
        app = create_app()
        with app.test_request_context():
            session["user_id"] = "inst-1"
            session["role"] = "instructor"
            html = render_template(
                "classes/class_reports.html",
                class_id="c1",
                class_name="Algebra",
                class_days="MW",
                class_section="01",
                students=[],
                learning_objectives=[],
                assignments=[],
            )
        class_json = html.find('id="classIdJson"')
        name_json = html.find('id="classNameJson"')
        vendor = html.find("jspdf.umd.min.js")
        plugin = html.find("jspdf.plugin.autotable.min.js")
        page_js = html.find("js/class_reports.js?v=2")
        shell = html.find("js/app_shell.js?v=4")
        self.assertGreater(class_json, -1)
        self.assertLess(class_json, name_json)
        self.assertLess(name_json, vendor)
        self.assertLess(vendor, plugin)
        self.assertLess(plugin, page_js)
        self.assertLess(page_js, shell)
        reports_css = html.find("css/class_reports_print.css?v=1")
        tailwind = html.find("css/tailwind.css")
        app_css = html.find("css/app.css")
        self.assertGreater(reports_css, -1)
        self.assertGreater(tailwind, -1)
        self.assertGreater(app_css, -1)
        self.assertLess(tailwind, app_css)
        self.assertLess(app_css, reports_css)
        self.assertLess(reports_css, page_js)
        self.assertNotIn("<style", html)
        self.assertIn("data-print", html)
        self.assertNotIn("instructorModeJson", html)
        self.assertIsNone(ON_ATTR.search(html))

    def test_scan_regexes_match_handlers_and_not_property_assignment(self):
        self.assertIsNotNone(JS_HANDLER.search('<button onclick="x">'))
        self.assertIsNotNone(JS_HANDLER.search("<button onclick='x'>"))
        self.assertIsNotNone(JS_HANDLER.search("el.setAttribute('onclick', 'x')"))
        self.assertIsNone(JS_HANDLER.search("el.onclick = function () {}"))
        self.assertIsNone(JS_HANDLER.search("el.onclick = null;"))
        self.assertIsNone(JS_HANDLER.search("var onlineStatus = 1;"))
        self.assertIsNotNone(JS_EVAL.search("eval(1)"))
        self.assertIsNotNone(JS_EVAL.search("new Function('x')"))
        self.assertIsNone(JS_EVAL.search("retrieval(1)"))
        self.assertIsNone(JS_EVAL.search("obj.myfunction(1)"))
        data_src = SCRIPT_OPEN.search('<script data-src="x">')
        real_src = SCRIPT_OPEN.search('<script src="x">')
        self.assertIsNone(SRC_ATTR.search(data_src.group(1)))
        self.assertIsNotNone(SRC_ATTR.search(real_src.group(1)))

    def test_every_browser_template_has_no_inline_script_handler_or_javascript_url(self):
        files = sorted((ROOT / "app/templates").rglob("*.html"))
        self.assertGreaterEqual(len(files), 21)
        json_scripts = 0
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(ON_ATTR.search(text), path.name)
            self.assertIsNone(re.search(r"javascript:", text, re.I), path.name)
            for match in SCRIPT_OPEN.finditer(text):
                attrs = match.group(1)
                if SRC_ATTR.search(attrs):
                    continue
                self.assertIn("application/json", attrs, path.name)
                json_scripts += 1
        self.assertGreater(json_scripts, 0)

    def test_page_scripts_have_no_eval_or_inline_handler_strings(self):
        files = sorted(path for path in (ROOT / "app/static/js").glob("*.js"))
        self.assertGreaterEqual(len(files), 19)
        self.assertTrue((ROOT / "app/static/js/vendor").is_dir())
        listeners = 0
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(JS_HANDLER.search(text), path.name)
            self.assertIsNone(re.search(r"javascript:", text, re.I), path.name)
            self.assertIsNone(JS_EVAL.search(text), path.name)
            listeners += text.count("addEventListener")
        self.assertGreater(listeners, 0)
