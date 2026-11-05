import os
import io
import re
import unittest
import unittest.mock

from app.models import Course
from app.helpers.csv_text import parse_students_csv_text
from app.helpers.student_names import COMMA_REQUIRED
from app import create_app
from tests.routes.fakes import _RemovalQuery


class _EmailTable:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.eq_calls = []
        self.in_calls = []
        self.ranges = []
        self.inserts = []
        self.updates = []
        self.selected = False
        self._page = None

    def select(self, *args, **kwargs):
        self.selected = True
        return self

    def eq(self, col, val):
        self.eq_calls.append((col, val))
        return self

    def in_(self, col, vals):
        self.in_calls.append((col, list(vals)))
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, count):
        return self

    def range(self, start, end):
        self.ranges.append((start, end))
        self._page = self.rows[start:end + 1]
        return self

    def insert(self, row):
        self.inserts.append(row)
        return self

    def update(self, row):
        self.updates.append(row)
        return self

    def execute(self):
        data = self.rows if self._page is None else self._page
        return unittest.mock.MagicMock(data=data)


class TestStudentEmailMatch(unittest.TestCase):
    def _client(self):
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        return client

    def _post_add(self, client, sa, body):
        from app.routes import students as r
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch(
                    "app.helpers.enrollment.supabase_admin", sa
                ), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True):
            return client.post(
                "/class/c1/add_student",
                json=body,
                headers={"X-CSRF-Token": "test-csrf"},
            )

    def _post_edit(self, client, sa, body, student_id="stu-1"):
        from app.routes import students as r
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch(
                    "app.helpers.enrollment.supabase_admin", sa
                ), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_student_enrolled_in_class", return_value=True):
            return client.post(
                "/api/class/c1/students/" + student_id + "/update",
                json=body,
                headers={"X-CSRF-Token": "test-csrf"},
            )

    def _sa(self, class_rows, enroll_rows):
        classes = _EmailTable(class_rows)
        enrollments = _EmailTable(enroll_rows)
        profiles = _EmailTable()
        sa = unittest.mock.MagicMock()
        sa.table.side_effect = lambda name: {
            "classes": classes,
            "enrollments": enrollments,
            "profiles": profiles,
        }[name]
        return sa, classes, enrollments, profiles

    def test_add_requires_email(self):
        client = self._client()
        sa, classes, enrollments, profiles = self._sa([], [])
        rv = self._post_add(client, sa, {"name": "Smith, Cali"})
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(rv.get_json()["error"], "Email and name are required")
        self.assertEqual(profiles.inserts, [])
        self.assertEqual(enrollments.inserts, [])

    def test_add_rejects_an_invalid_email(self):
        client = self._client()
        sa, classes, enrollments, profiles = self._sa([], [])
        rv = self._post_add(client, sa, {"name": "Smith, Cali", "email": "not-an-email"})
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(rv.get_json()["error"], "Invalid email format")
        sa.table.assert_not_called()

    def test_add_lowercases_the_email_on_insert(self):
        client = self._client()
        sa, classes, enrollments, profiles = self._sa([{"id": "c1"}], [])
        rv = self._post_add(
            client, sa, {"name": "Smith, Cali", "email": "  Cali@Example.EDU "},
        )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.get_json(), {
            "success": True,
            "full_name": "Smith, Cali",
        })
        self.assertEqual(profiles.inserts[0]["email"], "cali@example.edu")
        self.assertEqual(profiles.inserts[0]["full_name"], "Smith, Cali")
        self.assertEqual(enrollments.inserts[0]["class_id"], "c1")

    def test_add_email_already_in_this_class_inserts_nothing(self):
        client = self._client()
        sa, classes, enrollments, profiles = self._sa(
            [{"id": "c1"}],
            [{
                "class_id": "c1",
                "student_id": "stu-1",
                "profiles": {
                    "id": "stu-1",
                    "full_name": "Doe, Jane",
                    "email": "Jane@Example.EDU",
                },
            }],
        )
        rv = self._post_add(
            client, sa, {"name": "Smith, Ann", "email": "jane@example.edu"},
        )
        self.assertEqual(rv.status_code, 409)
        self.assertEqual(rv.get_json(), {
            "success": False,
            "error": "A student with that email is already in this class",
        })
        self.assertEqual(profiles.inserts, [])
        self.assertEqual(enrollments.inserts, [])

    def test_add_rejects_a_reused_email_when_the_name_differs(self):
        client = self._client()
        sa, classes, enrollments, profiles = self._sa(
            [{"id": "c1"}, {"id": "c-other"}],
            [{
                "class_id": "c-other",
                "student_id": "stu-1",
                "profiles": {
                    "id": "stu-1",
                    "full_name": "Doe, Jane",
                    "email": "jane@example.edu",
                },
            }],
        )
        rv = self._post_add(
            client, sa, {"name": "Smith, Ann", "email": "jane@example.edu"},
        )
        self.assertEqual(rv.status_code, 409)
        self.assertEqual(rv.get_json(), {
            "success": False,
            "error": (
                "That email belongs to Doe, Jane. "
                "Enter the name as Doe, Jane to add them to this class."
            ),
        })
        self.assertEqual(profiles.inserts, [])
        self.assertEqual(enrollments.inserts, [])

    def test_add_same_email_on_another_instructors_student_creates_a_profile(self):
        client = self._client()
        sa, classes, enrollments, profiles = self._sa([{"id": "c1"}], [])
        rv = self._post_add(
            client, sa, {"name": "Doe, Jane", "email": "jane@example.edu"},
        )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(profiles.inserts[0]["email"], "jane@example.edu")
        self.assertEqual(profiles.inserts[0]["full_name"], "Doe, Jane")
        self.assertFalse(profiles.selected)
        self.assertEqual(classes.eq_calls, [("instructor_id", "inst1")])
        self.assertEqual(enrollments.in_calls, [("class_id", ["c1"])])

    def test_email_index_reads_past_the_first_thousand_class_rows(self):
        client = self._client()
        class_rows = [{"id": "c%04d" % i} for i in range(1001)]
        sa, classes, enrollments, profiles = self._sa(class_rows, [])
        rv = self._post_add(
            client, sa, {"name": "Smith, Cali", "email": "cali@example.edu"},
        )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(classes.ranges, [(0, 999), (1000, 1999)])
        self.assertEqual(len(enrollments.in_calls[0][1]), 1001)

    def test_edit_rejects_a_blank_email(self):
        client = self._client()
        sa, classes, enrollments, profiles = self._sa([], [])
        rv = self._post_edit(client, sa, {"name": "Smith, Cali", "email": "  "})
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(rv.get_json()["error"], "Email and name are required")
        sa.table.assert_not_called()

    def test_edit_rejects_an_invalid_email(self):
        client = self._client()
        sa, classes, enrollments, profiles = self._sa([], [])
        rv = self._post_edit(client, sa, {"name": "Smith, Cali", "email": "not-an-email"})
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(rv.get_json()["error"], "Invalid email format")
        sa.table.assert_not_called()

    def test_edit_duplicate_inside_this_instructors_classes_is_409(self):
        client = self._client()
        sa, classes, enrollments, profiles = self._sa(
            [{"id": "c1"}],
            [{
                "class_id": "c1",
                "student_id": "stu-other",
                "profiles": {
                    "id": "stu-other",
                    "full_name": "Roe, Richard",
                    "email": "shared@example.edu",
                },
            }],
        )
        rv = self._post_edit(
            client, sa, {"name": "Smith, Cali", "email": "Shared@Example.EDU"},
        )
        self.assertEqual(rv.status_code, 409)
        self.assertEqual(rv.get_json(), {
            "success": False,
            "error": "That email already belongs to another student in your classes",
        })
        self.assertEqual(profiles.updates, [])

    def test_edit_duplicate_only_on_another_instructors_student_is_allowed(self):
        client = self._client()
        sa, classes, enrollments, profiles = self._sa([{"id": "c1"}], [])
        rv = self._post_edit(
            client, sa, {"name": "Smith, Cali", "email": "shared@example.edu"},
        )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(profiles.updates[0]["full_name"], "Smith, Cali")
        self.assertEqual(profiles.updates[0]["email"], "shared@example.edu")
        self.assertIn("email_set_at", profiles.updates[0])
        self.assertEqual(classes.eq_calls, [("instructor_id", "inst1")])
        self.assertTrue(profiles.selected)

    def test_csv_wizard_pushes_the_stored_name(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "static", "js", "csv_wizard.js",
        )
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("classRoster.push({ full_name: typed })", src)
        self.assertNotIn("var storedName = result.data.full_name;", src)

    def test_add_reuses_a_profile_when_the_name_key_matches(self):
        client = self._client()
        sa, classes, enrollments, profiles = self._sa(
            [{"id": "c1"}, {"id": "c-other"}],
            [{
                "class_id": "c-other",
                "student_id": "stu-1",
                "profiles": {
                    "id": "stu-1",
                    "full_name": "Doe, Jane",
                    "email": "jane@example.edu",
                },
            }],
        )
        rv = self._post_add(
            client, sa, {"name": "doe,  jane", "email": "jane@example.edu"},
        )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.get_json(), {
            "success": True,
            "full_name": "Doe, Jane",
        })
        self.assertEqual(profiles.inserts, [])
        self.assertEqual(enrollments.inserts, [{
            "class_id": "c1",
            "student_id": "stu-1",
        }])

    def _post_csv(self, client, sa, path, text):
        from app.routes import students as r
        payload = {"file": (io.BytesIO(text.encode("utf-8")), "students.csv")}
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch(
                    "app.helpers.enrollment.supabase_admin", sa
                ), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch("app.helpers.rate_limit._rate_limit", return_value=True):
            return client.post(
                path,
                data=payload,
                headers={"X-CSRF-Token": "test-csrf"},
            )

    def _jane_elsewhere(self):
        return self._sa(
            [{"id": "c1"}, {"id": "c-other"}],
            [{
                "class_id": "c-other",
                "student_id": "stu-1",
                "profiles": {
                    "id": "stu-1",
                    "full_name": "Doe, Jane",
                    "email": "jane@example.edu",
                },
            }],
        )

    def test_csv_email_already_in_this_class_writes_nothing(self):
        client = self._client()
        sa, classes, enrollments, profiles = self._sa(
            [{"id": "c1"}],
            [{
                "class_id": "c1",
                "student_id": "stu-1",
                "profiles": {
                    "id": "stu-1",
                    "full_name": "Doe, Jane",
                    "email": "jane@example.edu",
                },
            }],
        )
        text = (
            "Student Name,email\n"
            '"Doe, Jane",jane@example.edu\n'
            '"Smith, Ann",ann@example.edu\n'
        )
        rv = self._post_csv(client, sa, "/api/class/c1/upload_students", text)
        self.assertEqual(rv.status_code, 400)
        self.assertIn(
            "Row 2: jane@example.edu is already in this class.",
            rv.get_json()["errors"],
        )
        self.assertEqual(profiles.inserts, [])
        self.assertEqual(enrollments.inserts, [])

    def test_csv_reuses_a_profile_when_the_name_key_matches(self):
        client = self._client()
        sa, classes, enrollments, profiles = self._jane_elsewhere()
        text = 'Student Name,email\n"doe,  jane",jane@example.edu\n'
        preview = self._post_csv(
            client, sa, "/api/class/c1/preview-upload-students", text,
        )
        self.assertEqual(preview.status_code, 200)
        body = preview.get_json()
        self.assertNotIn("warnings", body)
        self.assertEqual(body["rows"], [{
            "full_name": "Doe, Jane",
            "email": "jane@example.edu",
            "action": "reuse",
            "status": "Will enroll",
        }])
        self.assertEqual(profiles.inserts, [])
        self.assertEqual(enrollments.inserts, [])
        upload = self._post_csv(client, sa, "/api/class/c1/upload_students", text)
        self.assertEqual(upload.status_code, 200)
        self.assertEqual(profiles.inserts, [])
        self.assertEqual(enrollments.inserts, [{
            "class_id": "c1",
            "student_id": "stu-1",
        }])
        path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "templates", "classes", "class_detail.html",
        )
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("studentUploadPreviewWarnings", src)
        self.assertNotIn("Some rows may have been skipped", src)

    def test_csv_rejects_a_reused_email_when_the_name_differs(self):
        client = self._client()
        sa, classes, enrollments, profiles = self._jane_elsewhere()
        text = (
            "Student Name,email\n"
            '"Smith, Ann",jane@example.edu\n'
            '"Smith, Bea",bea@example.edu\n'
        )
        sentence = (
            "Row 2: jane@example.edu belongs to Doe, Jane. "
            "Enter the name as Doe, Jane."
        )
        upload = self._post_csv(client, sa, "/api/class/c1/upload_students", text)
        preview = self._post_csv(
            client, sa, "/api/class/c1/preview-upload-students", text,
        )
        self.assertEqual(upload.status_code, 400)
        self.assertEqual(preview.status_code, 400)
        self.assertEqual(upload.get_json()["errors"], preview.get_json()["errors"])
        self.assertIn(sentence, upload.get_json()["errors"])
        self.assertEqual(profiles.inserts, [])
        self.assertEqual(enrollments.inserts, [])

    def test_csv_creates_a_profile_when_the_email_is_not_in_this_instructors_classes(self):
        client = self._client()
        sa, classes, enrollments, profiles = self._sa([{"id": "c1"}], [])
        text = 'Student Name,email\n"Smith, Cali",Cali@Example.EDU\n'
        rv = self._post_csv(client, sa, "/api/class/c1/upload_students", text)
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(profiles.inserts[0]["email"], "cali@example.edu")
        self.assertEqual(profiles.inserts[0]["full_name"], "Smith, Cali")
        self.assertEqual(profiles.inserts[0]["role"], "student")
        self.assertEqual(enrollments.inserts[0]["class_id"], "c1")
        self.assertEqual(enrollments.inserts[0]["student_id"], profiles.inserts[0]["id"])
        self.assertFalse(profiles.selected)
        self.assertIn(("instructor_id", "inst1"), classes.eq_calls)

    def test_csv_duplicate_email_in_the_file_writes_nothing(self):
        client = self._client()
        sa, classes, enrollments, profiles = self._sa([{"id": "c1"}], [])
        text = (
            "Student Name,email\n"
            '"Smith, Ann",ann@example.edu\n'
            '"Smith, Bea",ann@example.edu\n'
        )
        rv = self._post_csv(client, sa, "/api/class/c1/upload_students", text)
        self.assertEqual(rv.status_code, 400)
        self.assertIn(
            "Row 3: ann@example.edu is listed more than once.",
            rv.get_json()["errors"],
        )
        self.assertEqual(profiles.inserts, [])
        self.assertEqual(enrollments.inserts, [])

    def test_csv_rejects_a_missing_or_invalid_email_and_a_name_without_a_comma(self):
        client = self._client()
        samples = (
            (
                'Student Name,email\n"Smith, Ann",\n',
                "Row 2: Email is required.",
            ),
            (
                'Student Name,email\n"Smith, Ann",not-an-email\n',
                "Row 2: Invalid email format.",
            ),
            (
                "Student Name,email\nCali Smith,cali@example.edu\n",
                "Row 2: " + COMMA_REQUIRED,
            ),
        )
        for text, sentence in samples:
            sa, classes, enrollments, profiles = self._sa([{"id": "c1"}], [])
            rv = self._post_csv(client, sa, "/api/class/c1/upload_students", text)
            self.assertEqual(rv.status_code, 400)
            self.assertIn(sentence, rv.get_json()["errors"])
            self.assertEqual(profiles.inserts, [])
            self.assertEqual(enrollments.inserts, [])
            sa.table.assert_not_called()

    def test_add_student_and_csv_share_one_outcome(self):
        from app.helpers.enrollment import decide_student_enrollment
        index = {
            "jane@example.edu": {
                "profile_id": "stu-1",
                "full_name": "Doe, Jane",
                "class_ids": {"c-other"},
            },
        }
        self.assertEqual(
            decide_student_enrollment(index, "c1", "jane@example.edu", "Smith, Ann"),
            ("name_differs", "Doe, Jane", "stu-1"),
        )
        self.assertEqual(
            decide_student_enrollment(index, "c1", "jane@example.edu", "doe,  jane"),
            ("reuse", "Doe, Jane", "stu-1"),
        )
        client = self._client()
        sa, classes, enrollments, profiles = self._jane_elsewhere()
        added = self._post_add(
            client, sa, {"name": "Smith, Ann", "email": "jane@example.edu"},
        )
        uploaded = self._post_csv(
            client,
            sa,
            "/api/class/c1/upload_students",
            'Student Name,email\n"Smith, Ann",jane@example.edu\n',
        )
        self.assertEqual(added.status_code, 409)
        self.assertEqual(uploaded.status_code, 400)
        self.assertIn("Doe, Jane", added.get_json()["error"])
        self.assertIn("Doe, Jane", uploaded.get_json()["error"])
        self.assertEqual(profiles.inserts, [])
        self.assertEqual(enrollments.inserts, [])
        sa, classes, enrollments, profiles = self._jane_elsewhere()
        added = self._post_add(
            client, sa, {"name": "doe,  jane", "email": "jane@example.edu"},
        )
        self.assertEqual(added.status_code, 200)
        self.assertEqual(enrollments.inserts, [{
            "class_id": "c1",
            "student_id": "stu-1",
        }])
        sa, classes, enrollments, profiles = self._jane_elsewhere()
        uploaded = self._post_csv(
            client,
            sa,
            "/api/class/c1/upload_students",
            'Student Name,email\n"doe,  jane",jane@example.edu\n',
        )
        self.assertEqual(uploaded.status_code, 200)
        self.assertEqual(profiles.inserts, [])
        self.assertEqual(enrollments.inserts, [{
            "class_id": "c1",
            "student_id": "stu-1",
        }])

    def test_csv_trailing_commas_parse_as_one_row(self):
        rows, errors = parse_students_csv_text(
            'Student Name,email\n"Doe, Jane",jane@example.edu,,\n'
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["full_name"], "Doe, Jane")
        self.assertEqual(rows[0]["email"], "jane@example.edu")

    def test_csv_unquoted_comma_asks_for_quotes(self):
        rows, errors = parse_students_csv_text(
            "Student Name,email\nDoe, Jane,jane@example.edu\n"
        )
        self.assertEqual(rows, [])
        self.assertEqual(errors, [
            'Row 2: Put the name in quotes, like "Doe, Jane".',
        ])

    def test_csv_error_summary_caps_at_25_rows(self):
        client = self._client()
        sa, classes, enrollments, profiles = self._sa([{"id": "c1"}], [])
        lines = ["Student Name,email"]
        for i in range(30):
            lines.append('"Smith, Ann%s",' % i)
        text = "\n".join(lines) + "\n"
        rv = self._post_csv(client, sa, "/api/class/c1/upload_students", text)
        body = rv.get_json()
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(len(body["errors"]), 30)
        self.assertEqual(body["error"].count("Email is required."), 25)
        self.assertTrue(body["error"].endswith("\nand 5 more rows have errors."))
        self.assertEqual(profiles.inserts, [])
        self.assertEqual(enrollments.inserts, [])

    def _post_unenrolled(self, path, json_body=None):
        from app.routes import students as r
        client = self._client()
        sa = unittest.mock.MagicMock()
        query = unittest.mock.MagicMock()
        query.select.return_value = query
        query.eq.return_value = query
        query.delete.return_value = query
        query.update.return_value = query
        query.execute.return_value = unittest.mock.MagicMock(data=[])
        sa.table.return_value = query
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True):
            if json_body is None:
                rv = client.post(path, headers={"X-CSRF-Token": "test-csrf"})
            else:
                rv = client.post(
                    path,
                    json=json_body,
                    headers={"X-CSRF-Token": "test-csrf"},
                )
        return rv, query

    def test_delete_rejects_a_student_from_another_class(self):
        rv, query = self._post_unenrolled("/class/c1/students/stu-other/delete")
        self.assertEqual(rv.status_code, 403)
        self.assertEqual(rv.get_json()["error"], "Student not enrolled in this class")
        query.delete.assert_not_called()
        query.update.assert_not_called()

    def test_mute_rejects_a_student_from_another_class(self):
        rv, query = self._post_unenrolled(
            "/api/class/c1/toggle_mute",
            {"student_id": "stu-other", "muted": True},
        )
        self.assertEqual(rv.status_code, 403)
        self.assertEqual(rv.get_json()["error"], "Student not enrolled in this class")
        query.delete.assert_not_called()
        query.update.assert_not_called()

    def _post_enrolled(self, path, json_body=None):
        from app.routes import students as r
        client = self._client()
        sa = unittest.mock.MagicMock()
        query = unittest.mock.MagicMock()
        query.select.return_value = query
        query.eq.return_value = query
        query.delete.return_value = query
        query.update.return_value = query
        query.execute.return_value = unittest.mock.MagicMock(
            data=[{"student_id": "stu-1"}]
        )
        sa.table.return_value = query
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch("app.helpers.class_access.supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(Course, "get_all_lo_ids_for_class", return_value=[]):
            if json_body is None:
                rv = client.post(path, headers={"X-CSRF-Token": "test-csrf"})
            else:
                rv = client.post(
                    path,
                    json=json_body,
                    headers={"X-CSRF-Token": "test-csrf"},
                )
        return rv, query

    def test_delete_removes_an_enrolled_student(self):
        rv, query = self._post_enrolled("/class/c1/students/stu-1/delete")
        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.get_json()["success"])
        query.delete.assert_called()

    def test_mute_updates_an_enrolled_student(self):
        rv, query = self._post_enrolled(
            "/api/class/c1/toggle_mute",
            {"student_id": "stu-1", "muted": True},
        )
        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.get_json()["success"])
        query.update.assert_called()


class TestManageStudentDeleteAndEdit(unittest.TestCase):
    def _client(self):
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        return client

    def _removal_sa(self, log, fail_delete=None):
        grade_rows = [
            {
                "id": "g1",
                "student_id": "stu-1",
                "learning_objective_id": "lo-this",
                "assignment_id": "a1",
                "top_score": "M",
                "second_score": None,
                "counts_for_mastery": True,
            },
            {
                "id": "g2",
                "student_id": "stu-1",
                "learning_objective_id": "lo-this",
                "assignment_id": "a2",
                "top_score": "A",
                "second_score": None,
                "counts_for_mastery": False,
            },
        ]
        rows_for = {
            "grades": grade_rows,
            "homework_scores": [{"id": "h1"}],
            "free_passes": [],
            "enrollments": [{"id": "e1"}],
        }

        def table(name):
            return _RemovalQuery(log, name, rows_for.get(name, []), fail_delete)

        sa = unittest.mock.MagicMock()
        sa.table.side_effect = table
        return sa

    def _run_removal(self, method, path, log, fail_delete=None, owns=True, enrolled=True):
        from app.routes import students as r
        client = self._client()
        sa = self._removal_sa(log, fail_delete=fail_delete)
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch("app.helpers.class_removal.supabase_admin", sa), \
                unittest.mock.patch("app.helpers.grade_log.supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=owns), \
                unittest.mock.patch.object(r, "_student_enrolled_in_class", return_value=enrolled), \
                unittest.mock.patch.object(Course, "get_all_lo_ids_for_class", return_value=["lo-this"]):
            if method == "GET":
                return client.get(path)
            return client.post(path, headers={"X-CSRF-Token": "test-csrf"})

    def test_counts_equal_what_delete_removes(self):
        log = []
        counts = self._run_removal(
            "GET",
            "/api/class/c1/students/stu-1/removal-counts",
            log,
        )
        deleted = self._run_removal(
            "POST",
            "/class/c1/students/stu-1/delete",
            log,
        )
        self.assertEqual(counts.status_code, 200)
        body = counts.get_json()
        self.assertEqual(body["grade_rows"], 2)
        self.assertEqual(body["homework_scores"], 1)
        self.assertEqual(deleted.status_code, 200)
        count_filters = {e["table"]: e["filters"] for e in log if e["op"] == "count"}
        delete_filters = {e["table"]: e["filters"] for e in log if e["op"] == "delete"}
        self.assertEqual(count_filters, delete_filters)
        self.assertEqual(
            [e["table"] for e in log if e["op"] == "delete"],
            ["grades", "homework_scores", "free_passes", "enrollments"],
        )
        self.assertIn(("in", "learning_objective_id", ["lo-this"]), delete_filters["grades"])
        self.assertIn(("eq", "class_id", "c1"), delete_filters["homework_scores"])
        self.assertIn(("eq", "student_id", "stu-1"), delete_filters["homework_scores"])
        self.assertIn(("eq", "class_id", "c1"), delete_filters["enrollments"])
        self.assertIn(("eq", "student_id", "stu-1"), delete_filters["enrollments"])
        deleted_tables = [e["table"] for e in log if e["op"] == "delete"]
        self.assertNotIn("profiles", deleted_tables)
        self.assertTrue(any(e["table"] == "grade_change_log" and e["op"] == "insert" for e in log))
        self.assertFalse(any(e["table"] == "grade_change_log" and e["op"] == "delete" for e in log))
        from app.helpers.grade_log import GRADE_LOG_DELETE
        audit_rows = [
            row
            for entry in log
            if entry.get("table") == "grade_change_log" and entry.get("op") == "insert"
            for row in (entry.get("rows") or [])
        ]
        self.assertTrue(audit_rows)
        self.assertTrue(all(row["operation"] == GRADE_LOG_DELETE for row in audit_rows))
        for entry in log:
            for _op, _column, value in entry["filters"]:
                values = value if isinstance(value, list) else [value]
                self.assertNotIn("c2", values)

    def test_homework_delete_failure_leaves_the_enrollment(self):
        log = []
        rv = self._run_removal(
            "POST",
            "/class/c1/students/stu-1/delete",
            log,
            fail_delete="homework_scores",
        )
        self.assertEqual(rv.status_code, 500)
        self.assertEqual(
            [e["table"] for e in log if e["op"] in ("delete", "delete-failed")],
            ["grades", "homework_scores"],
        )
        self.assertNotIn("enrollments", [e["table"] for e in log if e["op"] == "delete"])

    def test_removal_counts_forbidden_when_the_class_is_not_owned(self):
        log = []
        rv = self._run_removal(
            "GET",
            "/api/class/c1/students/stu-1/removal-counts",
            log,
            owns=False,
        )
        self.assertEqual(rv.status_code, 403)
        self.assertEqual(log, [])

    def test_removal_counts_forbidden_when_the_student_is_not_enrolled(self):
        log = []
        rv = self._run_removal(
            "GET",
            "/api/class/c1/students/stu-1/removal-counts",
            log,
            enrolled=False,
        )
        self.assertEqual(rv.status_code, 403)
        self.assertEqual(rv.get_json()["error"], "Student not enrolled in this class")
        self.assertEqual(log, [])

    def test_edit_duplicate_email_does_not_update_the_profile(self):
        client = self._client()
        classes = _EmailTable([{"id": "c1"}])
        enrollments = _EmailTable([{
            "class_id": "c1",
            "student_id": "stu-other",
            "profiles": {
                "id": "stu-other",
                "full_name": "Roe, Richard",
                "email": "shared@example.edu",
            },
        }])
        profiles = _EmailTable()
        sa = unittest.mock.MagicMock()
        sa.table.side_effect = lambda name: {
            "classes": classes,
            "enrollments": enrollments,
            "profiles": profiles,
        }[name]
        from app.routes import students as r
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch(
                    "app.helpers.enrollment.supabase_admin", sa
                ), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_student_enrolled_in_class", return_value=True):
            rv = client.post(
                "/api/class/c1/students/stu-1/update",
                json={"name": "Smith, Cali", "email": "Shared@Example.EDU"},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 409)
        self.assertEqual(profiles.updates, [])

    def test_helper_rejects_the_same_name_and_email_on_add_and_edit(self):
        from app.routes import students as r
        cases = (
            ({"name": "Smith Cali", "email": "a@b.edu"}, COMMA_REQUIRED),
            ({"name": "Smith, Cali", "email": "not-an-email"}, "Invalid email format"),
            (
                {"name": ("N" * 300) + ", Ann", "email": "a@b.edu"},
                "Name must be 255 characters or fewer",
            ),
        )
        paths = (
            "/class/c1/add_student",
            "/api/class/c1/students/stu-1/update",
        )
        for path in paths:
            for body, error in cases:
                client = self._client()
                with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                        unittest.mock.patch.object(r, "_student_enrolled_in_class", return_value=True):
                    rv = client.post(
                        path,
                        json=body,
                        headers={"X-CSRF-Token": "test-csrf"},
                    )
                self.assertEqual(rv.status_code, 400)
                self.assertEqual(rv.get_json()["error"], error)

    def test_removed_student_route_is_missing(self):
        client = self._client()
        rv = client.post(
            "/api/class/c1/remove_student",
            json={"student_id": "stu-1"},
            headers={"X-CSRF-Token": "test-csrf"},
        )
        self.assertEqual(rv.status_code, 404)

    def test_manage_students_card_shows_the_active_count(self):
        from datetime import date
        from flask import render_template, session
        from app.helpers.revision_report import build_revision_report
        app = create_app()
        student = {
            "id": "stu-1",
            "name": "Lee, Ana",
            "email": "ana@example.edu",
            "learning_objectives": [],
            "objective_total": 0,
            "muted": False,
        }
        with app.test_request_context("/"):
            session["role"] = "instructor"
            html = render_template(
                "classes/class_detail.html",
                class_id="c1",
                class_name="Test Upload",
                class_days="MW",
                class_section="F26",
                students=[student],
                all_students=[student],
                learning_objectives=[],
                assignments=[],
                revision_report=build_revision_report([], date(2026, 10, 20)),
                revision_printed_on="2026-10-20",
            )
        self.assertIn("Manage Students", html)
        self.assertIn('id="activeStudentsCount"', html)
        match = re.search(r'id="activeStudentsCount"[^>]*>([^<]*)<', html)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1).strip(), "1")
