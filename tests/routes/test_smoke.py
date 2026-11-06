import os
import unittest
import unittest.mock
from unittest.mock import MagicMock

from app.models import Course, Homework
from app.helpers.class_removal import delete_class_rows
from app.helpers.report_email import _report_footer
from app.helpers.student_names import COMMA_REQUIRED
from app import create_app
from tests.routes.fakes import _RemovalQuery


class TestStudentDashboardRemoved(unittest.TestCase):
    """The student dashboard route is gone. Instructors get 404."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_authenticated_instructor_gets_404(self):
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        response = self.client.get("/student/dashboard")
        self.assertEqual(response.status_code, 404)


# ==========================================================================
# Reports assignment-scoping + email send
# ==========================================================================

CLASS_REPORTS_ID = "class-reports-scope"
ASG_NEW_ID = "asg-new-zero-grades"
ASG_OLD_ID = "asg-old-with-grades"
LO_D1_ID = "lo-d1"
LO_D2_ID = "lo-d2"
STU_ALEX_ID = "stu-alex"
STU_ALEX_EMAIL = "alex@example.edu"
STU_ALEX_NAME = "Alex Student"


class _FilterQuery:
    """Supabase-style chain that actually applies eq / is_ / in_ filters."""

    def __init__(self, rows):
        self._rows = [dict(r) for r in rows]
        self._limit = None
        self._range = None

    def select(self, *args, **kwargs):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def is_(self, col, val):
        if val is None:
            self._rows = [r for r in self._rows if r.get(col) is None]
        else:
            self._rows = [r for r in self._rows if r.get(col) is val]
        return self

    def in_(self, col, values):
        allowed = set(values)
        self._rows = [r for r in self._rows if r.get(col) in allowed]
        return self

    def gte(self, col, val):
        return self

    def lt(self, col, val):
        return self

    def limit(self, n):
        self._limit = n
        return self

    def order(self, *args, **kwargs):
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def execute(self):
        matched = list(self._rows)
        rows = matched
        if self._range is not None:
            start, end = self._range
            rows = matched[start:end + 1]
        elif self._limit is not None:
            rows = matched[:self._limit]
        result = MagicMock()
        result.data = rows
        result.count = len(matched)
        return result


def _filter_client(store):
    sa = MagicMock()

    def table(name):
        return _FilterQuery(store.get(name, []))

    sa.table.side_effect = table
    return sa


def _reports_scope_store():
    """Two assignments share D1/D2; only the older one has recorded grades."""
    new_aos = [
        {
            "assignment_id": ASG_NEW_ID,
            "learning_objective_id": LO_D1_ID,
            "learning_objectives": {"vendor_code": "D1"},
        },
        {
            "assignment_id": ASG_NEW_ID,
            "learning_objective_id": LO_D2_ID,
            "learning_objectives": {"vendor_code": "D2"},
        },
    ]
    old_aos = [
        {
            "assignment_id": ASG_OLD_ID,
            "learning_objective_id": LO_D1_ID,
            "learning_objectives": {"vendor_code": "D1"},
        },
        {
            "assignment_id": ASG_OLD_ID,
            "learning_objective_id": LO_D2_ID,
            "learning_objectives": {"vendor_code": "D2"},
        },
    ]
    return {
        "grades": [
            {
                "student_id": STU_ALEX_ID,
                "learning_objective_id": LO_D1_ID,
                "top_score": "M",
                "counts_for_mastery": True,
                "assignment_id": ASG_OLD_ID,
            },
            {
                "student_id": STU_ALEX_ID,
                "learning_objective_id": LO_D2_ID,
                "top_score": "R",
                "counts_for_mastery": True,
                "assignment_id": ASG_OLD_ID,
            },
        ],
        "assignment_objectives": new_aos + old_aos,
        "assignments": [
            {
                "id": ASG_NEW_ID,
                "class_id": CLASS_REPORTS_ID,
                "assignment_type": "project",
                "name": "Test Project",
            },
            {
                "id": ASG_OLD_ID,
                "class_id": CLASS_REPORTS_ID,
                "assignment_type": "exam",
                "name": "Exam 1",
            },
        ],
        "enrollments": [
            {
                "student_id": STU_ALEX_ID,
                "class_id": CLASS_REPORTS_ID,
                "profiles": {
                    "id": STU_ALEX_ID,
                    "full_name": STU_ALEX_NAME,
                    "email": STU_ALEX_EMAIL,
                },
            }
        ],
        "classes": [{
            "id": CLASS_REPORTS_ID,
            "name": "Algebra 1",
            "days": "",
            "section_number": "",
        }],
        "new_assignment_objectives": new_aos,
        "old_assignment_objectives": old_aos,
    }


def _assignment_scoped_grade_letter(grades_map, student_id, lo_id):
    """Same lookup as assignmentScopedGradeLetter in classes/class_reports.html."""
    key = f"{student_id}|{lo_id}"
    return str((grades_map or {}).get(key) or "").upper()


def _report_rows_from_scoped_grades(assignment_objectives, grades_map, student_id):
    """Same assignment-scoped branch as getIndividualReportParts in classes/class_reports.html."""
    rows = []
    for ao in assignment_objectives:
        lo_id = str(ao.get("learning_objective_id") or "")
        if not lo_id:
            continue
        letter = _assignment_scoped_grade_letter(grades_map, student_id, lo_id)
        nested = ao.get("learning_objectives") or {}
        title = str(nested.get("vendor_code") or "")
        rows.append({"title": title, "grade": letter or "Not graded"})
    return rows


def _email_body_from_report_rows(class_name, assignment_label, student_name, rows):
    """Same body shape as buildEmailPayloadForStudent in classes/class_reports.html."""
    lines = [
        "Hello,",
        "",
        f"Class: {class_name}",
        f"Assignment: {assignment_label}",
        "Date: 1/1/2026",
        f"Student: {student_name}",
        "",
        "Learning objectives and scores shown in this report:",
        "",
    ]
    for row in rows:
        title = " ".join(str(row.get("title") or "").split())
        lines.append(f"{title}  |  {row.get('grade') or 'Not graded'}")
    return "\n".join(lines)


class TestReportsAssignmentScopeAndEmail(unittest.TestCase):
    """Assignment-scoped grades API plus the report-email request/response path."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.store = _reports_scope_store()
        self.csrf = "test-csrf"

    def _login(self, *, user_id="inst1", role="instructor"):
        with self.client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["role"] = role
            sess["csrf_token"] = self.csrf

    def _headers(self):
        return {"X-CSRF-Token": self.csrf}

    def _grades_patches(self, *, owns_class=True, belongs=True):
        from app.routes import grading as r
        sa = _filter_client(self.store)
        return (
            unittest.mock.patch.object(r, "supabase_admin", sa),
            unittest.mock.patch.object(r, "_instructor_owns_class", return_value=owns_class),
            unittest.mock.patch.object(r, "_assignment_belongs_to_class", return_value=belongs),
            unittest.mock.patch.object(
                Homework, "get_hw_scores_map_for_assignment", return_value={}
            ),
            unittest.mock.patch.object(
                Homework, "get_hw_prev_scores_map_for_assignment", return_value={}
            ),
        )

    def _get_assignment_grades(self, assignment_id, *, owns_class=True, belongs=True):
        patches = self._grades_patches(owns_class=owns_class, belongs=belongs)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            return self.client.get(
                f"/api/class/{CLASS_REPORTS_ID}/assignment/{assignment_id}/grades"
            )

    def _post_single_email(self, payload, *, owns_class=True, send_result=(True, "")):
        from app.routes import class_email as r
        sa = _filter_client(self.store)
        send_mock = unittest.mock.MagicMock(return_value=send_result)
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch("app.helpers.report_email.supabase_admin", sa), \
                unittest.mock.patch("app.helpers.revision_mail.supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=owns_class), \
                unittest.mock.patch.object(r, "_send_via_resend", send_mock):
            rv = self.client.post(
                f"/api/class/{CLASS_REPORTS_ID}/student/{STU_ALEX_ID}/send-report-email",
                json=payload,
                headers=self._headers(),
            )
        return rv, send_mock

    def _post_bulk_email(self, payload, *, owns_class=True, send_result=(True, "")):
        from app.routes import class_email as r
        sa = _filter_client(self.store)
        send_mock = unittest.mock.MagicMock(return_value=send_result)
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch("app.helpers.report_email.supabase_admin", sa), \
                unittest.mock.patch("app.helpers.revision_mail.supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=owns_class), \
                unittest.mock.patch.object(r, "_send_via_resend", send_mock):
            rv = self.client.post(
                f"/api/class/{CLASS_REPORTS_ID}/send-report-emails",
                json=payload,
                headers=self._headers(),
            )
        return rv, send_mock

    def test_assignment_grades_unauthenticated_401(self):
        rv = self.client.get(
            f"/api/class/{CLASS_REPORTS_ID}/assignment/{ASG_NEW_ID}/grades"
        )
        self.assertEqual(rv.status_code, 401)

    def test_assignment_grades_forbidden_if_not_class_owner(self):
        self._login()
        rv = self._get_assignment_grades(ASG_NEW_ID, owns_class=False)
        self.assertEqual(rv.status_code, 403)

    def test_new_assignment_grades_map_is_empty(self):
        self._login()
        rv = self._get_assignment_grades(ASG_NEW_ID)
        self.assertEqual(rv.status_code, 200)
        body = rv.get_json()
        self.assertTrue(body.get("success"))
        self.assertEqual(body.get("grades"), {})
        self.assertEqual(body.get("counts_for_mastery_map"), {})

    def test_new_assignment_print_email_rows_are_not_graded(self):
        self._login()
        rv = self._get_assignment_grades(ASG_NEW_ID)
        grades_map = rv.get_json()["grades"]
        aggregate_los = [
            {"learning_objective_id": LO_D1_ID, "vendor_code": "D1", "top_score": "M"},
            {"learning_objective_id": LO_D2_ID, "vendor_code": "D2", "top_score": "R"},
        ]
        rows = _report_rows_from_scoped_grades(
            self.store["new_assignment_objectives"], grades_map, STU_ALEX_ID
        )
        self.assertEqual([row["grade"] for row in rows], ["Not graded", "Not graded"])
        self.assertEqual([row["title"] for row in rows], ["D1", "D2"])
        self.assertNotEqual(
            [row["grade"] for row in rows],
            [lo["top_score"] for lo in aggregate_los],
        )

    def test_shared_lo_query_does_not_return_sibling_assignment_grades(self):
        self._login()
        rv = self._get_assignment_grades(ASG_NEW_ID)
        grades_map = rv.get_json()["grades"]
        self.assertNotIn(f"{STU_ALEX_ID}|{LO_D1_ID}", grades_map)
        self.assertNotIn(f"{STU_ALEX_ID}|{LO_D2_ID}", grades_map)
        self.assertNotIn("M", grades_map.values())
        self.assertNotIn("R", grades_map.values())

    def test_shared_lo_sibling_assignment_returns_only_its_own_grades(self):
        self._login()
        rv = self._get_assignment_grades(ASG_OLD_ID)
        self.assertEqual(rv.status_code, 200)
        grades_map = rv.get_json()["grades"]
        self.assertEqual(
            grades_map,
            {
                f"{STU_ALEX_ID}|{LO_D1_ID}": "M",
                f"{STU_ALEX_ID}|{LO_D2_ID}": "R",
            },
        )
        new_rv = self._get_assignment_grades(ASG_NEW_ID)
        self.assertEqual(new_rv.get_json()["grades"], {})

    def test_send_report_email_unauthenticated_401(self):
        rv = self.client.post(
            f"/api/class/{CLASS_REPORTS_ID}/student/{STU_ALEX_ID}/send-report-email",
            json={"subject": "s", "body": "b"},
        )
        self.assertEqual(rv.status_code, 401)

    def test_send_report_email_non_instructor_401(self):
        self._login(role="student")
        rv, send_mock = self._post_single_email({"subject": "s", "body": "b"})
        self.assertEqual(rv.status_code, 401)
        send_mock.assert_not_called()

    def test_send_report_email_not_owner_403(self):
        self._login()
        rv, send_mock = self._post_single_email(
            {"subject": "s", "body": "b"}, owns_class=False
        )
        self.assertEqual(rv.status_code, 403)
        send_mock.assert_not_called()

    def test_send_report_email_rejects_missing_subject_or_body(self):
        self._login()
        rv, send_mock = self._post_single_email({"subject": "Progress", "body": ""})
        self.assertEqual(rv.status_code, 400)
        send_mock.assert_not_called()

    def test_send_report_email_uses_scoped_not_graded_content(self):
        self._login()
        grades_rv = self._get_assignment_grades(ASG_NEW_ID)
        grades_map = grades_rv.get_json()["grades"]
        rows = _report_rows_from_scoped_grades(
            self.store["new_assignment_objectives"], grades_map, STU_ALEX_ID
        )
        body = _email_body_from_report_rows(
            "Algebra 1", "Test Project", STU_ALEX_NAME, rows
        )
        subject = "Your progress report — Algebra 1"
        self.assertIn("D1  |  Not graded", body)
        self.assertIn("D2  |  Not graded", body)
        self.assertNotIn("D1  |  M", body)
        self.assertNotIn("D2  |  R", body)

        rv, send_mock = self._post_single_email({"subject": subject, "body": body})
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.get_json(), {"success": True, "to": STU_ALEX_EMAIL})
        sent_body = body + _report_footer("Your instructor", "Algebra 1")
        send_mock.assert_called_once_with(
            STU_ALEX_EMAIL, subject, sent_body, sender_name="Your instructor"
        )
        self.assertIn("D1  |  Not graded", send_mock.call_args[0][2])
        self.assertNotIn("D1  |  M", send_mock.call_args[0][2])

    def test_send_bulk_report_emails_uses_scoped_content(self):
        self._login()
        grades_rv = self._get_assignment_grades(ASG_NEW_ID)
        grades_map = grades_rv.get_json()["grades"]
        rows = _report_rows_from_scoped_grades(
            self.store["new_assignment_objectives"], grades_map, STU_ALEX_ID
        )
        body = _email_body_from_report_rows(
            "Algebra 1", "Test Project", STU_ALEX_NAME, rows
        )
        subject = "Your progress report — Algebra 1"
        rv, send_mock = self._post_bulk_email({
            "reports": [{
                "student_id": STU_ALEX_ID,
                "student_name": STU_ALEX_NAME,
                "subject": subject,
                "body": body,
            }]
        })
        self.assertEqual(rv.status_code, 200)
        payload = rv.get_json()
        self.assertTrue(payload.get("success"))
        self.assertEqual(payload.get("sent"), 1)
        sent_body = body + _report_footer("Your instructor", "Algebra 1")
        send_mock.assert_called_once_with(
            STU_ALEX_EMAIL, subject, sent_body, sender_name="Your instructor"
        )
        self.assertIn("Not graded", send_mock.call_args[0][2])
        self.assertNotIn("D1  |  M", sent_body)
        self.assertEqual(payload.get("failed"), 0)
        self.assertEqual(payload.get("skipped"), 0)

    def test_reports_template_print_email_uses_scoped_cache(self):
        template_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "app",
            "static",
            "js",
            "class_reports.js",
        )
        with open(template_path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("function assignmentScopedGradeLetter", src)
        self.assertIn("cached.gradesMap[key]", src)
        self.assertIn(
            "const scoped = assignmentScopedGradeLetter(student.id, loId, activeAssignmentId);",
            src,
        )
        self.assertIn("const grade = scoped || 'Not graded';", src)
        self.assertIn("/api/class/${classId}/student/${encodeURIComponent(studentId)}/send-report-email", src)
        self.assertIn("/api/class/${classId}/send-report-emails", src)

    def test_report_email_greeting_footer_and_counts(self):
        reports_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "static", "js", "class_reports.js",
        )
        history_script = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "static", "js", "student_history.js",
        )
        helpers_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "static", "js", "ui_helpers.js",
        )
        with open(reports_path, encoding="utf-8") as fh:
            reports_src = fh.read()
        with open(history_script, encoding="utf-8") as fh:
            history_src = fh.read()
        with open(helpers_path, encoding="utf-8") as fh:
            helpers_src = fh.read()

        def extract(src, name):
            marker = "function " + name + "("
            start = src.find(marker)
            self.assertGreaterEqual(start, 0, name)
            brace = src.find("{", start)
            depth = 0
            end = -1
            for i in range(brace, len(src)):
                if src[i] == "{":
                    depth += 1
                elif src[i] == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            self.assertGreaterEqual(end, 0, name)
            return src[start:end]

        builders = [
            extract(reports_src, "buildEmailPayloadForStudent"),
            extract(reports_src, "buildAllAssignmentsEmailPayloadForStudent"),
            extract(history_src, "emailStudentHistoryReport"),
        ]
        for body in builders:
            self.assertIn("studentGreetingName", body)
            self.assertNotIn("reportEmailFooter", body)
        combined = reports_src + history_src + helpers_src
        self.assertNotIn("function reportEmailFooter", helpers_src)
        self.assertNotIn("instructorLastName", combined)
        self.assertNotIn("instructorSignoff", combined)
        self.assertNotIn("toTitleCaseWord", combined)
        self.assertNotIn("Hello student,", combined)
        self.assertNotIn("via Project Clarity", combined)
        self.assertNotIn("Email send complete.", reports_src)
        self.assertIn(
            "Replies to this email are not monitored. Contact your instructor directly.",
            open(os.path.join(os.path.dirname(__file__), "..", "..", "app", "helpers", "report_email.py"), encoding="utf-8").read(),
        )
        self.assertIn("Sent ", reports_src)
        self.assertIn("Skipped ", reports_src)
        self.assertIn("Failed ", reports_src)


class TestMaxLengthValidation(unittest.TestCase):
    """Server-side max-length checks on free-text route inputs.

    Before fix: oversized strings were accepted and forwarded to the DB.
    After fix: each route returns 400 when input exceeds the defined limit,
    and no DB call is made.
    """

    def setUp(self):
        from app import create_app
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    # ── create_assignment ─────────────────────────────────────────────────

    def test_create_assignment_name_255_accepted(self):
        """Exactly 255 chars must be accepted (boundary)."""
        from app.routes import assignments as r
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["csrf_token"] = "test-csrf"
        sa = MagicMock()
        q = MagicMock()
        q.execute.return_value = MagicMock(data=[{"id": "asg-new"}])
        sa.table.return_value = q
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(
                    Homework, "canonicalize_homework_group_for_class", return_value="hw1"
                ), \
                unittest.mock.patch.object(Course, "get_lo_ids_for_class", return_value=[]):
            rv = self.client.post(
                "/class/c1/create_assignment",
                json={"name": "A" * 255, "homework_group": "hw1"},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertNotEqual(rv.status_code, 400)

    def test_create_assignment_name_256_rejected(self):
        """256 chars must be rejected with 400 and no DB call."""
        from app.routes import assignments as r
        sa = MagicMock()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(
                    Homework, "canonicalize_homework_group_for_class", return_value="hw1"
                ):
            rv = self.client.post(
                "/class/c1/create_assignment",
                json={"name": "A" * 256, "homework_group": "hw1"},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 400)
        self.assertIn("255", rv.get_json()["error"])
        sa.table.assert_not_called()

    def test_create_assignment_homework_group_101_rejected(self):
        """homework_group over 100 chars must be rejected with 400."""
        from app.routes import assignments as r
        sa = MagicMock()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(
                    Homework, "canonicalize_homework_group_for_class",
                    return_value="H" * 101,
                ):
            rv = self.client.post(
                "/class/c1/create_assignment",
                json={"name": "Quiz 1", "homework_group": "H" * 101},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 400)
        self.assertIn("100", rv.get_json()["error"])
        sa.table.assert_not_called()

    def _instructor_session(self):
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"

    def test_add_student_without_a_comma_rejects_before_the_database(self):
        from app.routes import students as r
        self._instructor_session()
        sa = MagicMock()
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True):
            rv = self.client.post(
                "/class/c1/add_student",
                json={"name": "Cali Smith", "email": "cali@example.edu"},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(rv.get_json()["error"], COMMA_REQUIRED)
        sa.table.assert_not_called()

    def test_add_student_stores_the_normalized_canvas_name(self):
        from app.routes import students as r
        self._instructor_session()
        sa = MagicMock()
        q = MagicMock()
        q.insert.return_value = q
        q.select.return_value = q
        q.eq.return_value = q
        q.order.return_value = q
        q.range.return_value = q
        q.in_.return_value = q
        q.execute.return_value = MagicMock(data=[])
        sa.table.return_value = q
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch(
                    "app.helpers.enrollment.supabase_admin", sa
                ), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True):
            rv = self.client.post(
                "/class/c1/add_student",
                json={"name": "Smith,  Cali", "email": "cali@example.edu"},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.get_json(), {
            "success": True,
            "full_name": "Smith, Cali",
        })
        profile_row = q.insert.call_args_list[0][0][0]
        self.assertEqual(profile_row["full_name"], "Smith, Cali")
        self.assertEqual(profile_row["email"], "cali@example.edu")
        self.assertEqual(profile_row["role"], "student")
        self.assertEqual(set(profile_row), {"id", "full_name", "role", "email", "email_set_at"})

    def test_add_student_rejects_a_256_character_comma_name_before_the_database(self):
        from app.routes import students as r
        self._instructor_session()
        sa = MagicMock()
        long_name = ("A" * 253) + ", Y"
        self.assertEqual(len(long_name), 256)
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True):
            rv = self.client.post(
                "/class/c1/add_student",
                json={"name": long_name, "email": "cali@example.edu"},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 400)
        self.assertIn("255", rv.get_json()["error"])
        sa.table.assert_not_called()

    def test_api_update_student_without_a_comma_does_not_update_the_profile(self):
        from app.routes import students as r
        self._instructor_session()
        sa = MagicMock()
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_student_enrolled_in_class", return_value=True):
            rv = self.client.post(
                "/api/class/c1/students/stu-1/update",
                json={"name": "Cali Smith", "email": "cali@example.edu"},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(rv.get_json()["error"], COMMA_REQUIRED)
        sa.table.assert_not_called()

    def test_api_update_student_writes_full_name_only(self):
        from app.routes import students as r
        self._instructor_session()
        sa = MagicMock()
        q = MagicMock()
        q.update.return_value = q
        q.eq.return_value = q
        q.select.return_value = q
        q.order.return_value = q
        q.range.return_value = q
        q.in_.return_value = q
        q.execute.return_value = MagicMock(data=[])
        sa.table.return_value = q
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch(
                    "app.helpers.enrollment.supabase_admin", sa
                ), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_student_enrolled_in_class", return_value=True):
            rv = self.client.post(
                "/api/class/c1/students/stu-1/update",
                json={"name": "Smith, Cali", "email": "Cali@Example.EDU"},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.get_json()["success"])
        payload = q.update.call_args[0][0]
        self.assertEqual(payload["full_name"], "Smith, Cali")
        self.assertEqual(payload["email"], "cali@example.edu")
        self.assertEqual(set(payload), {"full_name", "email", "email_set_at"})

    def _post_shared_profile_update(self, enrollments, classes):
        from app.routes import students as r
        updates = []
        store = {
            "classes": classes,
            "enrollments": enrollments,
            "profiles": [{
                "id": "s1",
                "email": "ana@school.edu",
                "full_name": "Lee, Ana",
            }],
        }

        class _Table:
            def __init__(self, name):
                self.name = name
                self.filters = []
                self.payload = None
                self.op = "select"

            def select(self, *args, **kwargs):
                return self

            def update(self, row):
                self.op = "update"
                self.payload = row
                return self

            def eq(self, col, val):
                self.filters.append((col, val))
                return self

            def in_(self, col, values):
                self.filters.append((col, list(values)))
                return self

            def order(self, *args, **kwargs):
                return self

            def limit(self, count):
                return self

            def range(self, start, end):
                return self

            def execute(self):
                if self.op == "update":
                    updates.append((self.name, self.payload))
                    result = MagicMock()
                    result.data = [self.payload]
                    return result
                rows = []
                for row in store.get(self.name, []):
                    keep = True
                    for col, val in self.filters:
                        if isinstance(val, list):
                            if row.get(col) not in val:
                                keep = False
                        elif row.get(col) != val:
                            keep = False
                    if keep:
                        rows.append(dict(row))
                result = MagicMock()
                result.data = rows
                result.count = len(rows)
                return result

        admin = MagicMock()
        admin.table.side_effect = lambda name: _Table(name)
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst-a"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "supabase_admin", admin), \
                unittest.mock.patch(
                    "app.helpers.enrollment.supabase_admin", admin
                ), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_student_enrolled_in_class", return_value=True):
            response = self.client.post(
                "/api/class/c1/students/s1/update",
                json={"name": "Lee, Ana", "email": "ana@school.edu"},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        return response, updates

    def _enrollment(self, enrollment_id, class_id, instructor_id):
        return {
            "id": enrollment_id,
            "class_id": class_id,
            "student_id": "s1",
            "profiles": {
                "id": "s1",
                "full_name": "Lee, Ana",
                "email": "ana@school.edu",
            },
            "classes": {"instructor_id": instructor_id},
        }

    def test_update_refuses_a_profile_shared_with_another_instructor(self):
        response, updates = self._post_shared_profile_update(
            [
                self._enrollment("e1", "c1", "inst-a"),
                self._enrollment("e2", "c-other", "inst-b"),
            ],
            [
                {"id": "c1", "instructor_id": "inst-a"},
                {"id": "c-other", "instructor_id": "inst-b"},
            ],
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.get_json()["error"],
            "This student is enrolled in another instructor's class. Nothing was changed.",
        )
        self.assertEqual(updates, [])

    def test_update_allows_a_profile_enrolled_only_in_the_callers_classes(self):
        response, updates = self._post_shared_profile_update(
            [
                self._enrollment("e1", "c1", "inst-a"),
                self._enrollment("e2", "c2", "inst-a"),
            ],
            [
                {"id": "c1", "instructor_id": "inst-a"},
                {"id": "c2", "instructor_id": "inst-a"},
            ],
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][0], "profiles")
        self.assertEqual(updates[0][1]["full_name"], "Lee, Ana")
        self.assertEqual(updates[0][1]["email"], "ana@school.edu")

    # ── api_update_learning_objective ─────────────────────────────────────

    def _lo_update_sa_mock(self):
        sa = MagicMock()
        q = MagicMock()
        q.select.return_value = q
        q.eq.return_value = q
        q.limit.return_value = q
        q.update.return_value = q
        q.execute.return_value = MagicMock(data=[{
            "id": "lo-1", "vendor_code": "D1", "description": None, "required_ms": 2,
        }])
        sa.table.return_value = q
        return sa

    def test_update_lo_vendor_code_50_accepted(self):
        """Exactly 50-char vendor_code must pass the length check."""
        from app.routes import assignments as r
        sa = self._lo_update_sa_mock()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_lo_vendor_code_conflict", return_value=False):
            rv = self.client.post(
                "/api/class/c1/update-lo/lo-1",
                json={"vendor_code": "A" * 50},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertNotEqual(rv.status_code, 400, "50-char code must not be length-rejected")

    def test_update_lo_vendor_code_51_rejected(self):
        """51-char vendor_code must be rejected with 400."""
        from app.routes import assignments as r
        sa = self._lo_update_sa_mock()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_lo_vendor_code_conflict", return_value=False):
            rv = self.client.post(
                "/api/class/c1/update-lo/lo-1",
                json={"vendor_code": "A" * 51},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 400)
        self.assertIn("50", rv.get_json()["error"])

    def test_update_lo_description_2000_accepted(self):
        """Exactly 2000-char description must pass the length check."""
        from app.routes import assignments as r
        sa = self._lo_update_sa_mock()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_lo_vendor_code_conflict", return_value=False):
            rv = self.client.post(
                "/api/class/c1/update-lo/lo-1",
                json={"vendor_code": "D1", "description": "x" * 2000},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertNotEqual(rv.status_code, 400, "2000-char description must not be length-rejected")

    def test_update_lo_description_2001_rejected(self):
        """2001-char description must be rejected with 400."""
        from app.routes import assignments as r
        sa = self._lo_update_sa_mock()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_lo_vendor_code_conflict", return_value=False):
            rv = self.client.post(
                "/api/class/c1/update-lo/lo-1",
                json={"vendor_code": "D1", "description": "x" * 2001},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 400)
        self.assertIn("2000", rv.get_json()["error"])

    # ── api_send_single_report_email ──────────────────────────────────────

    def _report_email_sa_mock(self):
        sa = MagicMock()
        q = MagicMock()
        q.select.return_value = q
        q.eq.return_value = q
        q.limit.return_value = q
        q.execute.return_value = MagicMock(data=[{
            "student_id": "s1",
            "profiles": {"id": "s1", "full_name": "Jane", "email": "jane@example.com"},
        }])
        sa.table.return_value = q
        return sa

    def test_report_email_subject_255_accepted(self):
        """Exactly 255-char subject must pass the length check."""
        from app.routes import class_email as r
        sa = self._report_email_sa_mock()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch("app.helpers.report_email.supabase_admin", sa), \
                unittest.mock.patch("app.helpers.revision_mail.supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch("app.helpers.rate_limit._rate_limit", return_value=True), \
                unittest.mock.patch.object(r, "_email_cap_counts", return_value=(0, {})), \
                unittest.mock.patch.object(r, "_send_via_resend", return_value=(True, None)):
            rv = self.client.post(
                "/api/class/c1/student/s1/send-report-email",
                json={"subject": "S" * 255, "body": "hello"},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertNotEqual(rv.status_code, 400, "255-char subject must not be length-rejected")

    def test_report_email_subject_256_rejected(self):
        """256-char subject must be rejected with 400; _send_via_resend must not be called."""
        from app.routes import class_email as r
        send_mock = unittest.mock.MagicMock()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch("app.helpers.rate_limit._rate_limit", return_value=True), \
                unittest.mock.patch.object(r, "_send_via_resend", send_mock):
            rv = self.client.post(
                "/api/class/c1/student/s1/send-report-email",
                json={"subject": "S" * 256, "body": "hello"},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 400)
        send_mock.assert_not_called()

    def test_report_email_body_5000_accepted(self):
        """Exactly 5 000-char body must pass the length check."""
        from app.routes import class_email as r
        sa = self._report_email_sa_mock()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch("app.helpers.report_email.supabase_admin", sa), \
                unittest.mock.patch("app.helpers.revision_mail.supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch("app.helpers.rate_limit._rate_limit", return_value=True), \
                unittest.mock.patch.object(r, "_email_cap_counts", return_value=(0, {})), \
                unittest.mock.patch.object(r, "_send_via_resend", return_value=(True, None)):
            rv = self.client.post(
                "/api/class/c1/student/s1/send-report-email",
                json={"subject": "subject", "body": "b" * 5000},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertNotEqual(rv.status_code, 400, "5 000-char body must not be length-rejected")

    def test_report_email_body_5001_rejected(self):
        """5 001-char body must be rejected with 400; _send_via_resend must not be called."""
        from app.routes import class_email as r
        send_mock = unittest.mock.MagicMock()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch("app.helpers.rate_limit._rate_limit", return_value=True), \
                unittest.mock.patch.object(r, "_send_via_resend", send_mock):
            rv = self.client.post(
                "/api/class/c1/student/s1/send-report-email",
                json={"subject": "subject", "body": "b" * 5001},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 400)
        send_mock.assert_not_called()


class TestClassDeleteRoutes(unittest.TestCase):
    def _client(self):
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        return client

    def _post(self, path, rows_for, owns=True):
        from app.routes import classes
        from app.routes import assignments
        from tests.patching import patch_attr
        log = []

        def table(name):
            return _RemovalQuery(log, name, rows_for.get(name, []), None)

        client = self._client()
        sa = unittest.mock.MagicMock()
        with patch_attr((classes, assignments), "supabase_admin", sa), \
                unittest.mock.patch("app.helpers.class_removal.supabase_admin", sa), \
                unittest.mock.patch("app.helpers.grade_log.supabase_admin", sa), \
                patch_attr(
                    (classes, assignments),
                    "_instructor_owns_class",
                    unittest.mock.Mock(return_value=owns),
                ):
            sa.table.side_effect = table
            rv = client.post(path, headers={"X-CSRF-Token": "test-csrf"})
        return rv, log

    def _run_rows(self, rows_for, fail_delete=None, fail_select=None):
        log = []
        self._log = log

        def table(name):
            return _RemovalQuery(
                log, name, rows_for.get(name, []), fail_delete, fail_select=fail_select
            )

        sa = unittest.mock.MagicMock()
        sa.table.side_effect = table
        with unittest.mock.patch("app.helpers.class_removal.supabase_admin", sa), \
                unittest.mock.patch("app.helpers.grade_log.supabase_admin", sa):
            delete_class_rows("c1", "inst1")
        return log

    def test_delete_class_rejects_a_non_owner_before_any_delete(self):
        rv, log = self._post("/class/c1/delete", {}, owns=False)
        self.assertEqual(rv.status_code, 403)
        self.assertEqual(rv.get_json()["error"], "Forbidden")
        self.assertEqual(log, [])

    def test_delete_class_owner_redirects_after_the_class_rows_are_deleted(self):
        rv, log = self._post(
            "/class/c1/delete",
            {
                "learning_objectives": [{"id": "lo-1"}],
                "assignments": [{"id": "a1"}],
                "grades": [{
                    "id": "g1",
                    "student_id": "stu-1",
                    "learning_objective_id": "lo-1",
                    "assignment_id": "a1",
                    "top_score": "M",
                    "second_score": None,
                    "counts_for_mastery": True,
                }],
            },
        )
        self.assertEqual(rv.status_code, 302)
        self.assertTrue(rv.headers["Location"].endswith("/instructor/dashboard"))
        deletes = [entry for entry in log if entry["op"] == "delete"]
        self.assertEqual([entry["table"] for entry in deletes], [
            "grades",
            "grades",
            "homework_scores",
            "free_passes",
            "assignment_objectives",
            "assignment_objectives",
            "assignments",
            "learning_objectives",
            "enrollments",
            "classes",
        ])
        self.assertIn(("in", "assignment_id", ["a1"]), deletes[0]["filters"])
        self.assertIn(("in", "learning_objective_id", ["lo-1"]), deletes[1]["filters"])
        self.assertIn(("eq", "class_id", "c1"), deletes[2]["filters"])
        self.assertIn(("eq", "class_id", "c1"), deletes[3]["filters"])
        self.assertIn(("in", "assignment_id", ["a1"]), deletes[4]["filters"])
        self.assertIn(("in", "learning_objective_id", ["lo-1"]), deletes[5]["filters"])
        self.assertIn(("eq", "class_id", "c1"), deletes[6]["filters"])
        self.assertIn(("eq", "class_id", "c1"), deletes[7]["filters"])
        self.assertIn(("eq", "class_id", "c1"), deletes[8]["filters"])
        self.assertIn(("eq", "id", "c1"), deletes[9]["filters"])
        self.assertTrue(any(
            entry["table"] == "grade_change_log" and entry["op"] == "insert" for entry in log
        ))
        self.assertFalse(any(
            entry["op"] == "delete" and entry["table"] in ("grade_change_log", "profiles")
            for entry in log
        ))

    def test_grades_are_deleted_by_assignment_and_objective_ids(self):
        log = self._run_rows({
            "learning_objectives": [{"id": "lo-1"}],
            "assignments": [{"id": "a1"}],
            "grades": [{"id": "g1", "student_id": "stu-1", "top_score": "M"}],
        })
        grade_deletes = [entry for entry in log if entry["op"] == "delete" and entry["table"] == "grades"]
        self.assertEqual(len(grade_deletes), 2)
        self.assertIn(("in", "assignment_id", ["a1"]), grade_deletes[0]["filters"])
        self.assertIn(("in", "learning_objective_id", ["lo-1"]), grade_deletes[1]["filters"])
        for table in ("homework_scores", "free_passes"):
            filters = next(
                entry["filters"] for entry in log if entry["op"] == "delete" and entry["table"] == table
            )
            self.assertIn(("eq", "class_id", "c1"), filters)

    def test_failing_id_lookup_raises_and_deletes_nothing(self):
        for table in ("learning_objectives", "assignments"):
            with self.assertRaises(RuntimeError):
                self._run_rows(
                    {"learning_objectives": [{"id": "lo-1"}], "assignments": [{"id": "a1"}]},
                    fail_select=table,
                )
            self.assertEqual([entry for entry in self._log if entry["op"] == "delete"], [])

    def test_assignment_delete_failure_stops_before_the_remaining_rows(self):
        with self.assertRaises(RuntimeError):
            self._run_rows(
                {"learning_objectives": [{"id": "lo-1"}], "assignments": [{"id": "a1"}]},
                fail_delete="assignments",
            )
        deleted = [entry["table"] for entry in self._log if entry["op"] == "delete"]
        self.assertNotIn("learning_objectives", deleted)
        self.assertNotIn("enrollments", deleted)
        self.assertNotIn("classes", deleted)

    def test_id_lists_longer_than_100_are_split(self):
        log = self._run_rows({
            "learning_objectives": [{"id": f"lo-{index}"} for index in range(101)],
            "assignments": [{"id": f"a-{index}"} for index in range(101)],
        })
        in_lists = [
            value
            for entry in log
            for kind, _column, value in entry["filters"]
            if kind == "in"
        ]
        self.assertTrue(in_lists)
        self.assertTrue(all(len(values) <= 100 for values in in_lists))
        self.assertIn(100, {len(values) for values in in_lists})
        self.assertIn(1, {len(values) for values in in_lists})

    def test_classes_row_is_the_last_delete(self):
        log = self._run_rows({
            "learning_objectives": [{"id": "lo-1"}],
            "assignments": [{"id": "a1"}],
        })
        deletes = [entry for entry in log if entry["op"] == "delete"]
        self.assertEqual(deletes[-1]["table"], "classes")
        self.assertIn(("eq", "id", "c1"), deletes[-1]["filters"])

    def test_delete_lo_from_another_class_returns_404_with_no_delete(self):
        rv, log = self._post("/class/c1/delete_lo/lo-other", {"learning_objectives": []})
        self.assertEqual(rv.status_code, 404)
        self.assertEqual(rv.get_json()["error"], "Learning objective not found")
        self.assertEqual([entry for entry in log if entry["op"] == "delete"], [])
        selects = [entry for entry in log if entry["op"] == "select"]
        self.assertEqual(len(selects), 1)
        self.assertIn(("eq", "id", "lo-other"), selects[0]["filters"])
        self.assertIn(("eq", "class_id", "c1"), selects[0]["filters"])

    def test_delete_lo_in_this_class_deletes_links_grades_and_the_objective(self):
        rv, log = self._post(
            "/class/c1/delete_lo/lo-1",
            {
                "learning_objectives": [{"id": "lo-1", "class_id": "c1"}],
                "grades": [{
                    "student_id": "stu-1",
                    "learning_objective_id": "lo-1",
                    "assignment_id": "a1",
                    "top_score": "M",
                    "second_score": None,
                    "counts_for_mastery": True,
                }],
            },
        )
        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.get_json()["success"])
        deleted = [entry["table"] for entry in log if entry["op"] == "delete"]
        self.assertEqual(deleted, [
            "assignment_objectives",
            "grades",
            "learning_objectives",
        ])

    def test_delete_assignment_from_another_class_returns_404(self):
        rv, log = self._post(
            "/class/c1/delete_assignment/asg-other",
            {"assignments": []},
        )
        self.assertEqual(rv.status_code, 404)
        self.assertEqual(rv.get_json()["error"], "Not found")
        self.assertEqual([entry for entry in log if entry["op"] == "delete"], [])
        selects = [entry for entry in log if entry["op"] == "select"]
        self.assertEqual(len(selects), 1)
        self.assertIn(("eq", "id", "asg-other"), selects[0]["filters"])
        self.assertIn(("eq", "class_id", "c1"), selects[0]["filters"])

    def test_delete_assignment_deletes_grades_for_linked_objectives_on_that_assignment(self):
        rv, log = self._post(
            "/class/c1/delete_assignment/a1",
            {
                "assignments": [{"id": "a1", "class_id": "c1"}],
                "assignment_objectives": [
                    {"assignment_id": "a1", "learning_objective_id": "lo-1"},
                ],
                "grades": [
                    {
                        "student_id": "stu-1",
                        "learning_objective_id": "lo-1",
                        "assignment_id": "a1",
                        "top_score": "M",
                        "second_score": None,
                        "counts_for_mastery": True,
                    },
                    {
                        "student_id": "stu-1",
                        "learning_objective_id": "lo-1",
                        "assignment_id": "a2",
                        "top_score": "M",
                        "second_score": None,
                        "counts_for_mastery": True,
                    },
                ],
            },
        )
        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.get_json()["success"])
        deleted = [entry["table"] for entry in log if entry["op"] == "delete"]
        self.assertEqual(deleted, ["grades", "assignment_objectives", "assignments"])
        grade_delete = next(
            entry for entry in log
            if entry["table"] == "grades" and entry["op"] == "delete"
        )
        self.assertIn(("eq", "assignment_id", "a1"), grade_delete["filters"])
        self.assertFalse(any(item[0] == "in" for item in grade_delete["filters"]))
        self.assertNotIn(("eq", "assignment_id", "a2"), grade_delete["filters"])
        self.assertNotIn("homework_scores", deleted)
