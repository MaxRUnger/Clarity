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
        self.assertLess(
            instructor.find('id="instructorModeJson"'),
            instructor.find("js/instructor_select_class.js"),
        )
        self.assertLess(
            instructor.find("js/instructor_select_class.js"),
            instructor.find("js/auth_theme.js"),
        )
        archived = pages["archived"]
        self.assertLess(archived.find("js/auth_theme.js"), archived.find("js/archived_classes.js"))
        students = pages["students"]
        self.assertLess(students.find("js/class_students.js"), students.find("<style"))
        self.assertLess(students.find("js/class_students.js"), students.find("js/app_shell.js"))
        detail = pages["detail"]
        self.assertLess(detail.find("js/class_student_detail.js"), detail.find("js/app_shell.js"))
        history = pages["history"]
        self.assertLess(
            history.find('id="studentHistoryEmailPayload"'),
            history.find("js/student_history.js"),
        )
        self.assertLess(history.find("js/student_history.js"), history.find("js/app_shell.js"))
        printed = pages["history_print"]
        self.assertLess(printed.find("js/student_history_print.js"), printed.find("<style"))
        objectives = pages["objectives"]
        self.assertLess(
            objectives.find("js/class_learning_objectives_summary.js"),
            objectives.find("<style"),
        )
        upload = pages["upload"]
        roster_at = upload.find('id="classRosterJson"')
        class_at = upload.find('id="classIdJson"')
        assignments_at = upload.find('id="assignmentsJson"')
        grade_at = upload.find("js/update_grade.js")
        wizard_at = upload.find("js/csv_wizard.js")
        shell_at = upload.find("js/app_shell.js")
        self.assertLess(roster_at, class_at)
        self.assertLess(class_at, assignments_at)
        self.assertLess(assignments_at, grade_at)
        self.assertLess(grade_at, wizard_at)
        self.assertLess(wizard_at, shell_at)
        self.assertIn("?v=21", upload[wizard_at:wizard_at + 80])
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
        page_js = html.find("js/class_assignments.js?v=1")
        config = html.find('id="loCsvConfig"')
        csv_js = html.find("js/class_assignments_lo_csv.js?v=1")
        mode_json = html.find('id="instructorModeJson"')
        mode_js = html.find("js/class_assignments_mode.js?v=1")
        shell = html.find("js/app_shell.js")
        self.assertLess(class_json, page_js)
        self.assertLess(page_js, config)
        self.assertLess(config, csv_js)
        self.assertLess(csv_js, mode_json)
        self.assertLess(mode_json, mode_js)
        self.assertLess(mode_js, shell)
