import unittest
import unittest.mock

from app.models import Course, Homework
from app import create_app
from tests.routes.fakes import _RemovalQuery, _class_detail_client


class TestApiAuthGuards(unittest.TestCase):
    """Instructor JSON APIs reject unauthenticated callers before body parsing."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_import_learning_objectives_unauthenticated_401(self):
        rv = self.client.post(
            "/api/class/test-class/import-learning-objectives",
            json={"rows": [{"vendor_code": "LO1", "description": "x", "required_ms": 2}]},
        )
        self.assertEqual(rv.status_code, 401)


class TestAssignmentHomeworkGroupRequired(unittest.TestCase):
    """create/update assignment reject a blank homework_group with a dedicated error."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def _session(self):
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        return {"X-CSRF-Token": "test-csrf"}

    def test_create_blank_homework_group_400(self):
        from app.routes import assignments as r
        headers = self._session()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(
                    Homework, "canonicalize_homework_group_for_class", return_value=None
                ):
            rv = self.client.post(
                "/class/class-1/create_assignment",
                json={"name": "Quiz 1", "homework_group": ""},
                headers=headers,
            )
        self.assertEqual(rv.status_code, 400)
        body = rv.get_json()
        self.assertFalse(body.get("success"))
        self.assertIn("homework group", (body.get("error") or "").lower())

    def test_update_blank_homework_group_400(self):
        from app.routes import assignments as r
        headers = self._session()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_assignment_belongs_to_class", return_value=True), \
                unittest.mock.patch.object(
                    Homework, "canonicalize_homework_group_for_class", return_value=None
                ):
            rv = self.client.post(
                "/class/class-1/assignments/asg-1/update",
                json={"name": "Quiz 1", "homework_group": ""},
                headers=headers,
            )
        self.assertEqual(rv.status_code, 400)
        body = rv.get_json()
        self.assertFalse(body.get("success"))
        self.assertIn("homework group", (body.get("error") or "").lower())


class TestUpdateAssignmentDetach(unittest.TestCase):
    def _client(self):
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        return client

    def _rows(self):
        return {
            "assignments": [{
                "id": "a1",
                "class_id": "c1",
                "homework_group": "Quiz 1",
            }],
            "learning_objectives": [{"id": "lo-1"}, {"id": "lo-2"}],
            "assignment_objectives": [
                {"learning_objective_id": "lo-1"},
                {"learning_objective_id": "lo-2"},
            ],
            "grades": [
                {
                    "student_id": "stu-1",
                    "learning_objective_id": "lo-2",
                    "assignment_id": "a1",
                    "top_score": "M",
                    "second_score": None,
                    "counts_for_mastery": True,
                },
                {
                    "student_id": "stu-1",
                    "learning_objective_id": "lo-2",
                    "assignment_id": "a2",
                    "top_score": "M",
                    "second_score": None,
                    "counts_for_mastery": True,
                },
                {
                    "student_id": "stu-1",
                    "learning_objective_id": "lo-1",
                    "assignment_id": "a1",
                    "top_score": "M",
                    "second_score": None,
                    "counts_for_mastery": True,
                },
            ],
        }

    def _send(self, method, path, rows_for, body=None, owns=True, fail_delete=None, fail_insert=None, fail_update=None):
        from app.routes import assignments as r
        log = []

        def table(name):
            return _RemovalQuery(
                log,
                name,
                rows_for.get(name, []),
                fail_delete,
                fail_insert=fail_insert,
                fail_update=fail_update,
            )

        client = self._client()
        with unittest.mock.patch.object(r, "supabase_admin") as sa, \
                unittest.mock.patch(
                    "app.helpers.assignment_rows.supabase_admin", sa
                ), \
                unittest.mock.patch("app.helpers.grade_log.supabase_admin", sa), \
                unittest.mock.patch("app.helpers.class_access.supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=owns), \
                unittest.mock.patch.object(
                    Course, "get_lo_ids_for_class", return_value=["lo-1", "lo-2"]
                ):
            sa.table.side_effect = table
            headers = {"X-CSRF-Token": "test-csrf"}
            if method == "PUT":
                rv = client.put(path, json=body or {}, headers=headers)
            else:
                rv = client.get(path, headers=headers)
        return rv, log

    def _body(self, selected):
        return {
            "name": "Quiz",
            "homework_group": "Quiz 1",
            "selected_los": selected,
            "assignment_type": "mastery_opp",
        }

    def _link_writes(self, log):
        return [
            entry for entry in log
            if entry["table"] == "assignment_objectives"
            and entry["op"] in ("insert", "delete", "insert-failed", "delete-failed")
        ]

    def test_same_objectives_make_no_link_writes(self):
        rv, log = self._send(
            "PUT",
            "/class/c1/assignments/a1/update",
            self._rows(),
            self._body(["lo-1", "lo-2"]),
        )
        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.get_json()["success"])
        self.assertEqual(self._link_writes(log), [])
        self.assertFalse(any(
            entry["table"] == "grades" and entry["op"] in ("delete", "delete-failed")
            for entry in log
        ))

    def test_adding_one_objective_inserts_only_that_link(self):
        rows = self._rows()
        rows["assignment_objectives"] = [{"learning_objective_id": "lo-1"}]
        rv, log = self._send(
            "PUT",
            "/class/c1/assignments/a1/update",
            rows,
            self._body(["lo-1", "lo-2"]),
        )
        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.get_json()["success"])
        inserts = [
            entry for entry in log
            if entry["table"] == "assignment_objectives" and entry["op"] == "insert"
        ]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0]["rows"], [{
            "assignment_id": "a1",
            "learning_objective_id": "lo-2",
        }])
        self.assertFalse(any(
            entry["table"] == "assignment_objectives" and entry["op"] == "delete"
            for entry in log
        ))
        self.assertFalse(any(
            entry["table"] == "grades" and entry["op"] == "delete"
            for entry in log
        ))

    def test_removing_one_objective_deletes_only_that_link_then_its_grades(self):
        rv, log = self._send(
            "PUT",
            "/class/c1/assignments/a1/update",
            self._rows(),
            self._body(["lo-1"]),
        )
        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.get_json()["success"])
        link_deletes = [
            entry for entry in log
            if entry["table"] == "assignment_objectives" and entry["op"] == "delete"
        ]
        self.assertEqual(len(link_deletes), 1)
        self.assertIn(("eq", "assignment_id", "a1"), link_deletes[0]["filters"])
        self.assertIn(("in", "learning_objective_id", ["lo-2"]), link_deletes[0]["filters"])
        self.assertFalse(any(
            entry["table"] == "assignment_objectives" and entry["op"] == "insert"
            for entry in log
        ))
        grade_delete = next(
            entry for entry in log
            if entry["table"] == "grades" and entry["op"] == "delete"
        )
        self.assertGreater(
            log.index(grade_delete),
            log.index(link_deletes[0]),
        )
        self.assertIn(("eq", "assignment_id", "a1"), grade_delete["filters"])
        self.assertIn(("in", "learning_objective_id", ["lo-2"]), grade_delete["filters"])
        self.assertNotIn(("eq", "assignment_id", "a2"), grade_delete["filters"])
        self.assertNotIn("homework_scores", [entry["table"] for entry in log])

    def test_failed_insert_deletes_nothing(self):
        rows = self._rows()
        rows["assignment_objectives"] = [{"learning_objective_id": "lo-1"}]
        rv, log = self._send(
            "PUT",
            "/class/c1/assignments/a1/update",
            rows,
            self._body(["lo-1", "lo-2"]),
            fail_insert="assignment_objectives",
        )
        self.assertEqual(rv.status_code, 500)
        self.assertEqual(rv.get_json()["error"], "Could not update assignment")
        self.assertTrue(any(
            entry["table"] == "assignment_objectives" and entry["op"] == "insert-failed"
            for entry in log
        ))
        self.assertEqual(
            [entry for entry in log if entry["op"] in ("delete", "delete-failed")],
            [],
        )
        self.assertTrue(any(
            entry["table"] == "assignments" and entry["op"] == "update"
            for entry in log
        ))

    def test_failed_link_delete_deletes_no_grades(self):
        rv, log = self._send(
            "PUT",
            "/class/c1/assignments/a1/update",
            self._rows(),
            self._body(["lo-1"]),
            fail_delete="assignment_objectives",
        )
        self.assertEqual(rv.status_code, 500)
        self.assertEqual(rv.get_json()["error"], "Could not update assignment")
        self.assertTrue(any(
            entry["table"] == "assignment_objectives" and entry["op"] == "delete-failed"
            for entry in log
        ))
        self.assertEqual(
            [
                entry for entry in log
                if entry["table"] == "grades" and entry["op"] in ("delete", "delete-failed")
            ],
            [],
        )

    def test_failed_link_rewrite_does_not_delete_grades(self):
        rv, log = self._send(
            "PUT",
            "/class/c1/assignments/a1/update",
            self._rows(),
            self._body(["lo-1"]),
            fail_delete="assignment_objectives",
        )
        self.assertEqual(rv.status_code, 500)
        self.assertEqual(rv.get_json()["error"], "Could not update assignment")
        grade_ops = [
            entry for entry in log
            if entry["table"] == "grades" and entry["op"] in ("delete", "delete-failed")
        ]
        self.assertEqual(grade_ops, [])

    def test_failed_grade_delete_returns_its_own_error(self):
        rv, log = self._send(
            "PUT",
            "/class/c1/assignments/a1/update",
            self._rows(),
            self._body(["lo-1"]),
            fail_delete="grades",
        )
        self.assertEqual(rv.status_code, 500)
        self.assertEqual(
            rv.get_json()["error"],
            "Could not delete grades for removed objectives",
        )
        self.assertTrue(any(
            entry["table"] == "assignment_objectives" and entry["op"] == "delete"
            for entry in log
        ))
        self.assertTrue(any(
            entry["table"] == "grades" and entry["op"] == "delete-failed"
            for entry in log
        ))

    def test_non_owner_update_is_forbidden_before_any_write(self):
        rv, log = self._send(
            "PUT",
            "/class/c1/assignments/a1/update",
            self._rows(),
            self._body(["lo-1"]),
            owns=False,
        )
        self.assertEqual(rv.status_code, 403)
        self.assertEqual(rv.get_json()["error"], "Forbidden")
        self.assertEqual(log, [])

    def test_assignment_from_another_class_is_rejected_before_any_write(self):
        rows = self._rows()
        rows["assignments"] = []
        rv, log = self._send(
            "PUT",
            "/class/c1/assignments/a1/update",
            rows,
            self._body(["lo-1"]),
        )
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(rv.get_json()["error"], "Invalid assignment for class")
        self.assertEqual([entry for entry in log if entry["op"] == "delete"], [])

    def test_missing_name_is_rejected_before_any_write(self):
        body = self._body(["lo-1"])
        body["name"] = ""
        rv, log = self._send(
            "PUT",
            "/class/c1/assignments/a1/update",
            self._rows(),
            body,
        )
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(rv.get_json()["error"], "Assignment name is required.")
        self.assertEqual([entry for entry in log if entry["op"] == "delete"], [])

    def test_detach_count_requires_class_ownership(self):
        rv, log = self._send(
            "GET",
            "/api/class/c1/assignments/a1/detach-grade-count?lo_id=lo-2",
            self._rows(),
            owns=False,
        )
        self.assertEqual(rv.status_code, 403)
        self.assertEqual(rv.get_json()["error"], "Forbidden")
        self.assertEqual(log, [])

    def test_detach_count_of_zero_is_returned_for_the_owner(self):
        rows = self._rows()
        rows["grades"] = []
        rv, log = self._send(
            "GET",
            "/api/class/c1/assignments/a1/detach-grade-count?selected_lo=lo-1&selected_lo=lo-2",
            rows,
        )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.get_json()["grade_rows"], 0)
        self.assertNotIn("homework_scores", [entry["table"] for entry in log])

    def test_orphan_grades_are_deleted_when_the_removed_set_is_empty(self):
        rows = self._rows()
        rows["assignment_objectives"] = [{"learning_objective_id": "lo-1"}]
        rows["grades"] = [
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
                "learning_objective_id": "lo-9",
                "assignment_id": "a1",
                "top_score": "M",
                "second_score": None,
                "counts_for_mastery": True,
            },
            {
                "student_id": "stu-1",
                "learning_objective_id": "lo-9",
                "assignment_id": "a2",
                "top_score": "M",
                "second_score": None,
                "counts_for_mastery": True,
            },
        ]
        rv, log = self._send(
            "PUT",
            "/class/c1/assignments/a1/update",
            rows,
            self._body(["lo-1"]),
        )
        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.get_json()["success"])
        self.assertEqual(self._link_writes(log), [])
        grade_delete = next(
            entry for entry in log
            if entry["table"] == "grades" and entry["op"] == "delete"
        )
        self.assertIn(("eq", "assignment_id", "a1"), grade_delete["filters"])
        self.assertIn(("in", "learning_objective_id", ["lo-9"]), grade_delete["filters"])
        self.assertNotIn(("eq", "assignment_id", "a2"), grade_delete["filters"])

    def test_detach_count_and_deletion_agree(self):
        rows = self._rows()
        rows["assignment_objectives"] = [{"learning_objective_id": "lo-1"}]
        rows["grades"] = [
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
                "learning_objective_id": "lo-9",
                "assignment_id": "a1",
                "top_score": "M",
                "second_score": None,
                "counts_for_mastery": True,
            },
            {
                "student_id": "stu-1",
                "learning_objective_id": "lo-9",
                "assignment_id": "a2",
                "top_score": "M",
                "second_score": None,
                "counts_for_mastery": True,
            },
        ]
        counted, _count_log = self._send(
            "GET",
            "/api/class/c1/assignments/a1/detach-grade-count?selected_lo=lo-1",
            rows,
        )
        saved, save_log = self._send(
            "PUT",
            "/class/c1/assignments/a1/update",
            rows,
            self._body(["lo-1"]),
        )
        self.assertEqual(counted.status_code, 200)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(counted.get_json()["grade_rows"], 1)
        grade_delete = next(
            entry for entry in save_log
            if entry["table"] == "grades" and entry["op"] == "delete"
        )
        self.assertEqual(
            counted.get_json()["grade_rows"],
            len(next(
                item[2] for item in grade_delete["filters"] if item[0] == "in"
            )),
        )
        self.assertIn(("in", "learning_objective_id", ["lo-9"]), grade_delete["filters"])
        self.assertIn(("eq", "assignment_id", "a1"), grade_delete["filters"])
        self.assertNotIn(("eq", "assignment_id", "a2"), grade_delete["filters"])

    def test_failed_row_update_writes_no_links_and_deletes_no_grades(self):
        rv, log = self._send(
            "PUT",
            "/class/c1/assignments/a1/update",
            self._rows(),
            self._body(["lo-2"]),
            fail_update="assignments",
        )
        self.assertEqual(rv.status_code, 500)
        self.assertEqual(rv.get_json()["error"], "Could not update assignment")
        self.assertTrue(any(
            entry["table"] == "assignments" and entry["op"] == "update-failed"
            for entry in log
        ))
        self.assertEqual(self._link_writes(log), [])
        self.assertEqual(
            [
                entry for entry in log
                if entry["table"] == "grades" and entry["op"] in ("delete", "delete-failed")
            ],
            [],
        )


class TestAssignmentsPageReads(unittest.TestCase):
    CLASS_ID = "class-asg"
    EMPTY_ID = "class-no-los"
    CALLS = [
        "classes", "classes", "learning_objectives",
        "assignments", "assignment_objectives",
    ]
    EMPTY_CALLS = [
        "classes", "classes", "learning_objectives", "assignments",
    ]

    def _class_row(self, class_id, name, instructor_id="inst-1"):
        return {
            "id": class_id, "instructor_id": instructor_id, "name": name,
            "semester": "Fall", "days": "MWF", "section_number": "1",
            "auto_convert_m": False, "is_online": False,
        }

    def _objective(self, lo_id, class_id, vendor_code, name, required_ms):
        return {
            "id": lo_id, "class_id": class_id, "vendor_code": vendor_code,
            "name": name, "description": "%s text" % name,
            "required_ms": required_ms,
        }

    def _link(self, assignment_id, lo_id, vendor_code, description):
        return {
            "assignment_id": assignment_id,
            "learning_objective_id": lo_id,
            "assignments": {"class_id": self.CLASS_ID},
            "learning_objectives": {
                "id": lo_id, "vendor_code": vendor_code, "description": description,
            },
        }

    def _assignment(self, assignment_id, name, homework_group, created_at, links, assignment_type):
        return {
            "id": assignment_id, "class_id": self.CLASS_ID, "name": name,
            "homework_group": homework_group, "date_returned": "2026-10-17",
            "revision_due": "2026-10-24", "created_at": created_at,
            "assignment_type": assignment_type, "assignment_objectives": links,
        }

    def _enrollment(self, class_id, student_id, full_name, muted):
        return {
            "id": "enr-%s" % student_id, "class_id": class_id,
            "student_id": student_id, "muted": muted,
            "profiles": {
                "id": student_id, "full_name": full_name,
                "role": "student", "email": "%s@example.edu" % student_id,
            },
        }

    def _store(self):
        link_b = self._link("asg-linked", "lo-1", "B2", "Beta text")
        link_a = self._link("asg-linked", "lo-2", "A1", "Alpha text")
        bare_links = []
        return {
            "classes": [self._class_row(self.CLASS_ID, "Algebra")],
            "learning_objectives": [
                self._objective("lo-1", self.CLASS_ID, "B2", "Beta", 2),
                self._objective("lo-2", self.CLASS_ID, "A1", "Alpha", 1),
                self._objective("lo-9", self.CLASS_ID, "C3", "Gamma", 3),
            ],
            "enrollments": [
                self._enrollment(self.CLASS_ID, "stu-ada", "Ada, Ann", False),
                self._enrollment(self.CLASS_ID, "stu-mute", "Mute, Mo", True),
            ],
            "grades": [
                {
                    "id": "g-ada", "student_id": "stu-ada",
                    "learning_objective_id": "lo-2", "top_score": "M",
                    "second_score": None, "counts_for_mastery": True,
                },
                {
                    "id": "g-mute", "student_id": "stu-mute",
                    "learning_objective_id": "lo-1", "top_score": "R",
                    "second_score": None, "counts_for_mastery": True,
                },
            ],
            "assignment_objectives": [link_b, link_a],
            "assignments": [
                self._assignment(
                    "asg-bare", "Bare Exam", "", "2026-10-02", bare_links, "exam",
                ),
                self._assignment(
                    "asg-linked", "Linked Quiz", "HW 1", "2026-10-01",
                    [link_b, link_a], "mastery_opp",
                ),
            ],
        }

    def _page(self, store, class_id):
        from flask import template_rendered
        from app import models
        from app.routes import assignments as r
        calls, selects = [], []
        admin = _class_detail_client(store, calls, selects)
        app = create_app()
        app.config["TESTING"] = True
        captured = {}

        def grab(sender, template, context, **kwargs):
            if getattr(template, "name", "") == "classes/class_assignments.html":
                captured["context"] = context

        template_rendered.connect(grab, app)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst-1"
            sess["role"] = "instructor"
        try:
            with unittest.mock.patch.object(r, "supabase_admin", admin), \
                    unittest.mock.patch.object(models, "supabase_admin", admin), \
                    unittest.mock.patch("app.helpers.class_progress.supabase_admin", admin), \
                    unittest.mock.patch("app.helpers.class_data.supabase_admin", admin), \
                    unittest.mock.patch("app.helpers.class_access.supabase_admin", admin):
                response = client.get("/class/%s/assignments" % class_id)
        finally:
            template_rendered.disconnect(grab, app)
        return response, calls, captured.get("context")

    def _objective_rows(self, page):
        import re
        return re.findall(
            r'data-lo-id="([^"]*)"\s+data-lo-code="([^"]*)"\s+'
            r'data-lo-description="([^"]*)"\s+data-lo-required="([^"]*)"',
            page,
        )

    def _checkbox_rows(self, page):
        import re
        block = page.split('id="standardLoSelect"', 1)[1].split("</div>", 1)[0]
        return re.findall(
            r'value="([^"]*)" data-lo-id="([^"]*)">\s*'
            r'<span class="text-xs text-slate-700 dark:text-slate-300">([^<]*)</span>',
            block,
        )

    def _chips(self, page):
        import re
        return re.findall(
            r'bg-blue-100 dark:bg-blue-900 text-blue-800 dark:text-blue-200 '
            r'text-xs rounded-full">\s*([^<]+?)\s*</span>',
            page,
        )

    def _edit_buttons(self, page):
        import json
        found = []
        marker = "data-assignment='"
        start = 0
        while True:
            at = page.find(marker, start)
            if at < 0:
                break
            at += len(marker)
            end = page.find("'>", at)
            found.append(json.loads(page[at:end]))
            start = end + 2
        return found

    def _edit_fields(self, assignment):
        return {
            "id": assignment["id"],
            "name": assignment["name"],
            "homework_group": assignment.get("homework_group"),
            "date_returned": assignment.get("date_returned"),
            "revision_due": assignment.get("revision_due"),
            "assignment_type": assignment.get("assignment_type"),
            "learning_objective_ids": [
                link.get("learning_objective_id")
                for link in (assignment.get("assignment_objectives") or [])
            ],
        }

    def test_owned_class_pins_objectives_assignments_and_ignores_students(self):
        response, calls, context = self._page(self._store(), self.CLASS_ID)
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, self.CALLS)
        self.assertEqual(context["class_name"], "Algebra")
        self.assertEqual(context["unattached_count"], 1)
        self.assertEqual(
            [
                (lo["id"], lo["vendor_code"], lo["description"], lo["required_ms"])
                for lo in context["all_los"]
            ],
            [
                ("lo-2", "A1", "Alpha text", 1),
                ("lo-1", "B2", "Beta text", 2),
                ("lo-9", "C3", "Gamma text", 3),
            ],
        )
        self.assertEqual(self._objective_rows(page), [
            ("lo-2", "A1", "Alpha text", "1"),
            ("lo-1", "B2", "Beta text", "2"),
            ("lo-9", "C3", "Gamma text", "3"),
        ])
        self.assertEqual(self._checkbox_rows(page), [
            ("lo-2", "lo-2", "A1"),
            ("lo-1", "lo-1", "B2"),
            ("lo-9", "lo-9", "C3"),
        ])
        self.assertIn("1 objective not attached to an assignment yet", page)
        self.assertEqual(self._chips(page), ["A1", "B2"])
        expected_edits = [
            {
                "id": "asg-linked", "name": "Linked Quiz", "homework_group": "HW 1",
                "date_returned": "2026-10-17", "revision_due": "2026-10-24",
                "assignment_type": "mastery_opp",
                "learning_objective_ids": ["lo-2", "lo-1"],
            },
            {
                "id": "asg-bare", "name": "Bare Exam", "homework_group": "",
                "date_returned": "2026-10-17", "revision_due": "2026-10-24",
                "assignment_type": "exam",
                "learning_objective_ids": [],
            },
        ]
        self.assertEqual(
            [self._edit_fields(row) for row in context["assignments"]],
            expected_edits,
        )
        self.assertEqual(
            [self._edit_fields(row) for row in self._edit_buttons(page)],
            expected_edits,
        )
        self.assertIn("HW 1", page)
        self.assertIn(
            '<script id="classIdJson" type="application/json">"%s"</script>' % self.CLASS_ID,
            page,
        )
        self.assertNotIn("Ada, Ann", page)
        self.assertNotIn("Mute, Mo", page)
        self.assertNotIn("ada@example.edu", page)
        self.assertNotIn("mute@example.edu", page)

    def test_no_objectives_skips_the_attached_id_read(self):
        store = {
            "classes": [self._class_row(self.EMPTY_ID, "Empty")],
            "learning_objectives": [],
            "enrollments": [
                self._enrollment(self.EMPTY_ID, "stu-zoe", "Zoe, Ann", False),
            ],
            "grades": [],
            "assignment_objectives": [],
            "assignments": [],
        }
        response, calls, context = self._page(store, self.EMPTY_ID)
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, self.EMPTY_CALLS)
        self.assertEqual(context["all_los"], [])
        self.assertEqual(context["unattached_count"], 0)
        self.assertEqual(context["assignments"], [])
        self.assertEqual(self._objective_rows(page), [])
        self.assertEqual(self._checkbox_rows(page), [])
        self.assertNotIn("not attached to an assignment yet", page)
        self.assertNotIn("assignment_objectives", calls)
        self.assertNotIn("grades", calls)

    def test_other_instructors_class_redirects_home(self):
        store = {
            "classes": [self._class_row(self.CLASS_ID, "Algebra", instructor_id="inst-other")],
            "learning_objectives": [], "enrollments": [], "grades": [],
            "assignment_objectives": [], "assignments": [],
        }
        response, calls, context = self._page(store, self.CLASS_ID)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/instructor/dashboard", response.headers["Location"])
        self.assertEqual(calls, ["classes"])
        self.assertIsNone(context)


class TestCreateLearningObjectiveRole(unittest.TestCase):
    def _get(self, role):
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = role
        return client

    def test_student_owner_is_sent_to_login(self):
        client = self._get("student")
        with unittest.mock.patch("app.routes.assignments._instructor_owns_class", return_value=True), \
                unittest.mock.patch("app.routes.assignments.Course.get_full_class_data") as load:
            load.return_value = {"id": "c1"}
            response = client.get("/class/c1/create_learning_objective")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        load.assert_not_called()

    def test_instructor_get_still_redirects_to_assignments(self):
        client = self._get("instructor")
        with unittest.mock.patch("app.routes.assignments._instructor_owns_class", return_value=True), \
                unittest.mock.patch(
                    "app.routes.assignments.Course.get_full_class_data",
                    return_value={"id": "c1"},
                ):
            response = client.get("/class/c1/create_learning_objective")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/class/c1/assignments", response.headers["Location"])
