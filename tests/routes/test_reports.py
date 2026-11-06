import os
import re
import unittest

from app import create_app


class TestStudentHistoryOrder(unittest.TestCase):
    def _render_history(self, grades, assignments):
        import json
        from flask import session
        from unittest.mock import MagicMock, patch
        from app.routes.reports import student_history

        self.grade_selects = []
        self.grade_eqs = []
        self.grade_orders = []
        self.grade_ranges = []

        def table(name):
            chain = MagicMock()
            if name == "enrollments":
                chain.select.return_value.eq.return_value.eq.return_value.single.return_value.execute.return_value.data = {
                    "student_id": "s1",
                    "profiles": {"id": "p1", "full_name": "Lee, Ana", "email": "a@b.edu"},
                }
            elif name == "grades":
                def select(cols):
                    self.grade_selects.append(cols)
                    return chain

                def eq(col, val):
                    self.grade_eqs.append((col, val))
                    return chain

                def order(col, desc=False):
                    self.grade_orders.append(col)
                    return chain

                def range_call(start, end):
                    self.grade_ranges.append((start, end))
                    chain._page = (start, end)
                    return chain

                def execute():
                    start, end = chain._page
                    result = MagicMock()
                    result.data = grades[start:end + 1]
                    return result

                chain.select.side_effect = select
                chain.eq.side_effect = eq
                chain.in_.return_value = chain
                chain.order.side_effect = order
                chain.range.side_effect = range_call
                chain.execute.side_effect = execute
            return chain

        app = create_app()
        with app.test_request_context("/class/c1/student/s1/history"):
            session["user_id"] = "u1"
            session["role"] = "instructor"
            with patch("app.routes.reports._instructor_owns_class", return_value=True), \
                 patch("app.models.Course.get_full_class_data", return_value={
                     "name": "Test Upload",
                     "days": "MW",
                     "section_number": "F26",
                     "learning_objectives": [{
                         "id": "lo1",
                         "vendor_code": "D1",
                         "description": "",
                     }],
                 }), \
                 patch("app.routes.reports.objectives_attached_to_assignments", side_effect=lambda class_id, pool: list(pool or [])), \
                 patch("app.routes.reports.load_assignments_for_class", return_value=assignments) as load_assignments, \
                 patch("app.routes.reports._report_sender_name", return_value="Estes"), \
                 patch("app.routes.reports.supabase_admin") as sb:
                sb.table.side_effect = table
                html = student_history("c1", "s1")
        payload_match = re.search(
            r'id="studentHistoryEmailPayload"[^>]*>(.*?)</script>',
            html,
            re.S,
        )
        self.assertIsNotNone(payload_match)
        payload = json.loads(payload_match.group(1))
        return html, load_assignments, payload["objectives"][0]["grades"]

    def _screen_names_and_scores(self, html):
        screen = html.split("student-history-print-table", 1)[0]
        names = [
            name.strip()
            for name in re.findall(r'text-sm font-medium[^>]*>\s*([^<]+)', screen)
        ]
        scores = [
            score.strip()
            for score in re.findall(r'mastery-badge[^>]*>\s*([^<]+)', screen)
        ]
        return names, scores

    def _print_rows(self, html):
        printed = html.split("student-history-print-table", 1)[1]
        rows = re.findall(
            r'class="assignment-name">\s*([^<]+?)\s*</td>.*?class="grade-cell">\s*([^<]*?)\s*</td>',
            printed,
            re.S,
        )
        return [(name.strip(), score.strip()) for name, score in rows]

    def test_history_template_does_not_reverse_grades(self):
        page_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "templates", "classes", "student_history.html",
        )
        table_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "templates", "classes", "student_history_table.html",
        )
        with open(page_path, encoding="utf-8") as handle:
            page = handle.read()
        with open(table_path, encoding="utf-8") as handle:
            table = handle.read()
        source = page + table
        self.assertNotIn("|reverse", source)
        self.assertIn("{% for grade in lo.grades %}", source)
        self.assertIn("{% for cell in row.cells %}", source)

    def test_history_follows_class_assignment_list(self):
        grades = [
            {
                "learning_objective_id": "lo1",
                "assignment_id": "a-late",
                "top_score": "R",
                "second_score": None,
                "assignments": {"name": "Quiz"},
            },
            {
                "learning_objective_id": "lo1",
                "assignment_id": "a-early",
                "top_score": "M",
                "second_score": None,
                "assignments": {"name": "Quiz"},
            },
        ]
        assignments = [
            {"id": "a-early", "name": "Quiz", "created_at": "2026-01-01"},
            {"id": "a-late", "name": "Quiz", "created_at": "2026-02-01"},
        ]
        html, load_assignments, emailed = self._render_history(grades, assignments)
        load_assignments.assert_called_once_with("c1")
        names, scores = self._screen_names_and_scores(html)
        self.assertEqual(names, ["Quiz", "Quiz"])
        self.assertEqual(scores, ["M", "R"])
        self.assertEqual(self._print_rows(html), [("Quiz", "M"), ("Quiz", "R")])
        self.assertEqual(
            [grade.get("assignment_id") for grade in emailed],
            ["a-early", "a-late"],
        )
        self.assertEqual([grade.get("top_score") for grade in emailed], ["M", "R"])

    def test_duplicate_grades_for_one_assignment_are_both_kept(self):
        grades = [
            {
                "learning_objective_id": "lo1",
                "assignment_id": "a-early",
                "top_score": "M",
                "second_score": None,
                "assignments": {"name": "Quiz"},
            },
            {
                "learning_objective_id": "lo1",
                "assignment_id": "a-early",
                "top_score": "R",
                "second_score": None,
                "assignments": {"name": "Quiz"},
            },
        ]
        assignments = [
            {"id": "a-early", "name": "Quiz", "created_at": "2026-01-01"},
        ]
        html, _load_assignments, emailed = self._render_history(grades, assignments)
        names, scores = self._screen_names_and_scores(html)
        self.assertEqual(names, ["Quiz", "Quiz"])
        self.assertEqual(scores, ["M", "R"])
        self.assertEqual(
            [grade.get("assignment_id") for grade in emailed],
            ["a-early", "a-early"],
        )
        self.assertEqual([grade.get("top_score") for grade in emailed], ["M", "R"])

    def test_null_assignment_id_is_last_and_has_no_print_row(self):
        grades = [
            {
                "learning_objective_id": "lo1",
                "assignment_id": "a-late",
                "top_score": "R",
                "second_score": None,
                "assignments": {"name": "Quiz"},
            },
            {
                "learning_objective_id": "lo1",
                "assignment_id": "a-early",
                "top_score": "M",
                "second_score": None,
                "assignments": {"name": "Quiz"},
            },
            {
                "learning_objective_id": "lo1",
                "assignment_id": None,
                "top_score": "X",
                "second_score": None,
                "assignments": None,
            },
        ]
        assignments = [
            {"id": "a-early", "name": "Quiz", "created_at": "2026-01-01"},
            {"id": "a-late", "name": "Quiz", "created_at": "2026-02-01"},
        ]
        html, _load_assignments, emailed = self._render_history(grades, assignments)
        names, scores = self._screen_names_and_scores(html)
        self.assertEqual(names, ["Quiz", "Quiz", "Assignment"])
        self.assertEqual(scores, ["M", "R", "X"])
        self.assertEqual(self._print_rows(html), [("Quiz", "M"), ("Quiz", "R")])
        self.assertEqual(
            [grade.get("assignment_id") for grade in emailed],
            ["a-early", "a-late", None],
        )
        self.assertEqual([grade.get("top_score") for grade in emailed], ["M", "R", "X"])

    def test_empty_assignment_list_keeps_only_the_null_assignment_grade(self):
        grades = [
            {
                "learning_objective_id": "lo1",
                "assignment_id": "a-early",
                "top_score": "M",
                "second_score": None,
                "assignments": {"name": "Quiz"},
            },
            {
                "learning_objective_id": "lo1",
                "assignment_id": "a-late",
                "top_score": "R",
                "second_score": None,
                "assignments": {"name": "Later"},
            },
            {
                "learning_objective_id": "lo1",
                "assignment_id": None,
                "top_score": "X",
                "second_score": None,
                "assignments": None,
            },
        ]
        html, load_assignments, emailed = self._render_history(grades, [])
        load_assignments.assert_called_once_with("c1")
        names, scores = self._screen_names_and_scores(html)
        self.assertEqual(names, ["Assignment"])
        self.assertEqual(scores, ["X"])
        self.assertEqual(self._print_rows(html), [])
        self.assertEqual(
            [grade.get("assignment_id") for grade in emailed],
            [None],
        )
        self.assertEqual([grade.get("top_score") for grade in emailed], ["X"])

    def test_outside_assignment_is_absent_and_null_assignment_stays_last(self):
        grades = [
            {
                "learning_objective_id": "lo1",
                "assignment_id": None,
                "top_score": "X",
                "second_score": None,
                "assignments": None,
            },
            {
                "learning_objective_id": "lo1",
                "assignment_id": "z-out",
                "top_score": "P",
                "second_score": None,
                "assignments": {"name": "Outside"},
            },
            {
                "learning_objective_id": "lo1",
                "assignment_id": "a-early",
                "top_score": "M",
                "second_score": None,
                "assignments": {"name": "Quiz"},
            },
        ]
        assignments = [
            {"id": "a-early", "name": "Quiz", "created_at": "2026-01-01"},
        ]
        html, _load_assignments, emailed = self._render_history(grades, assignments)
        names, scores = self._screen_names_and_scores(html)
        self.assertEqual(names, ["Quiz", "Assignment"])
        self.assertEqual(scores, ["M", "X"])
        self.assertEqual(self._print_rows(html), [("Quiz", "M")])
        self.assertNotIn("Outside", html)
        self.assertEqual(
            [grade.get("assignment_id") for grade in emailed],
            ["a-early", None],
        )
        self.assertEqual([grade.get("top_score") for grade in emailed], ["M", "X"])

    def test_history_grade_read_pages_across_two_pages(self):
        from app.helpers.paging import PAGE_SIZE
        grades = [
            {
                "id": "g%04d" % index,
                "learning_objective_id": "lo1",
                "assignment_id": "a1",
                "top_score": "M",
                "second_score": None,
                "assignments": {"name": "Quiz"},
            }
            for index in range(PAGE_SIZE)
        ]
        grades.append({
            "id": "g-last",
            "learning_objective_id": "lo1",
            "assignment_id": "a1",
            "top_score": "R",
            "second_score": None,
            "assignments": {"name": "Quiz"},
        })
        grades.append({
            "id": "g-other",
            "learning_objective_id": "lo1",
            "assignment_id": "a-other",
            "top_score": "P",
            "second_score": None,
            "assignments": {"name": "Other Class"},
        })
        assignments = [{"id": "a1", "name": "Quiz", "created_at": "2026-01-01"}]
        _html, _load_assignments, emailed = self._render_history(grades, assignments)
        self.assertEqual(
            self.grade_selects,
            [(
                "learning_objective_id, assignment_id, top_score, "
                "second_score, assignments(name)"
            )],
        )
        self.assertEqual(self.grade_eqs, [("student_id", "p1")])
        self.assertEqual(self.grade_orders, ["id"])
        self.assertEqual(
            self.grade_ranges,
            [(0, PAGE_SIZE - 1), (PAGE_SIZE, (PAGE_SIZE * 2) - 1)],
        )
        self.assertEqual(len(emailed), PAGE_SIZE + 1)
        self.assertEqual(emailed[-1]["id"], "g-last")
        self.assertEqual(emailed[-1]["top_score"], "R")
        self.assertNotIn("a-other", [grade.get("assignment_id") for grade in emailed])

    def _batch_supabase(self, reads, enrollments, grades, assignments, objectives, fail=None):
        from unittest.mock import MagicMock

        class_row = {
            "id": "c1",
            "instructor_id": "prof-1",
            "name": "Test Upload",
            "days": "MW",
            "section_number": "F26",
        }
        attached = [
            {"assignment_id": "a-early", "learning_objective_id": lo["id"]}
            for lo in objectives
            if lo.get("vendor_code") != "ZZ"
        ]

        def table(name):
            chain = MagicMock()
            flags = {"single": False}

            def mark_single():
                flags["single"] = True
                return chain

            chain.single.side_effect = mark_single
            for method in ("select", "eq", "in_", "order", "limit", "range"):
                getattr(chain, method).return_value = chain

            def execute():
                reads["n"] += 1
                if fail == name:
                    raise RuntimeError("database unavailable")
                if name == "classes":
                    data = [class_row]
                elif name == "assignments":
                    data = assignments
                elif name == "enrollments" and flags["single"]:
                    data = enrollments[0] if enrollments else None
                elif name == "enrollments":
                    data = enrollments
                elif name == "learning_objectives":
                    data = objectives
                elif name == "assignment_objectives":
                    data = attached
                elif name == "grades":
                    data = grades
                else:
                    data = []
                return MagicMock(data=data)

            chain.execute.side_effect = execute
            return chain

        admin = MagicMock()
        admin.table.side_effect = table
        return admin

    def _render_batch(self, enrollments, grades, assignments, query, objectives=None, fail=None):
        from flask import session
        from unittest.mock import patch
        from app.routes.reports import student_history_print

        if objectives is None:
            objectives = [{
                "id": "lo1",
                "name": "Derivatives",
                "vendor_code": "D1",
                "description": "Derivatives",
                "required_ms": 1,
            }]
        reads = {"n": 0}
        admin = self._batch_supabase(
            reads, enrollments, grades, assignments, objectives, fail=fail,
        )
        app = create_app()
        path = "/class/c1/student-history/print" + query
        with app.test_request_context(path):
            session["user_id"] = "prof-1"
            session["role"] = "instructor"
            with patch("app.routes.reports.supabase_admin", admin), \
                 patch("app.helpers.class_progress.supabase_admin", admin), \
                 patch("app.helpers.history_packet.supabase_admin", admin), \
                 patch("app.helpers.class_data.supabase_admin", admin), \
                 patch("app.helpers.class_access.supabase_admin", admin), \
                 patch("app.models.supabase_admin", admin):
                rendered = student_history_print("c1")
        if isinstance(rendered, tuple):
            body, status = rendered[0], rendered[1]
        else:
            body, status = rendered, 200
        return body, status, reads["n"]

    def _print_table(self, html):
        start = html.find('<div class="student-history-print-table">')
        self.assertGreaterEqual(start, 0)
        end = html.find("</div>", start)
        self.assertGreaterEqual(end, 0)
        return html[start:end + len("</div>")]

    def test_reports_header_has_print_all_history(self):
        from flask import render_template, session
        app = create_app()
        with app.test_request_context("/"):
            session["role"] = "instructor"
            html = render_template(
                "classes/class_reports.html",
                class_id="c1",
                class_name="Test Upload",
                class_days="MW",
                class_section="F26",
                students=[],
                learning_objectives=[],
                assignments=[],
                instructor_title="Professor Estes",
            )
        self.assertIn("Print all history", html)
        self.assertIn("/class/c1/student-history/print?print=1", html)
        self.assertNotIn("instructorTitleJson", html)

    def test_batch_page_lists_active_students_and_keeps_empty_history(self):
        enrollments = [
            {"muted": False, "profiles": {"id": "s-zebra", "full_name": "Zebra, Zoe", "email": ""}},
            {"muted": True, "profiles": {"id": "s-muted", "full_name": "Quinn, Quin", "email": ""}},
            {"muted": False, "profiles": {"id": "s-adams", "full_name": "Adams, Amy", "email": ""}},
            {"muted": False, "profiles": {"id": "s-baker", "full_name": "Baker, Bo", "email": ""}},
        ]
        grades = [
            {
                "student_id": "s-adams",
                "learning_objective_id": "lo1",
                "assignment_id": "a-late",
                "top_score": "R",
                "second_score": None,
            },
            {
                "student_id": "s-adams",
                "learning_objective_id": "lo1",
                "assignment_id": "a-early",
                "top_score": "M",
                "second_score": None,
            },
            {
                "student_id": "s-muted",
                "learning_objective_id": "lo1",
                "assignment_id": "a-early",
                "top_score": "P",
                "second_score": None,
            },
            {
                "student_id": "stranger",
                "learning_objective_id": "lo1",
                "assignment_id": "a-early",
                "top_score": "X",
                "second_score": None,
            },
        ]
        assignments = [
            {
                "id": "a-early",
                "name": "Quiz",
                "created_at": "2026-01-01",
                "assignment_objectives": [],
            },
            {
                "id": "a-late",
                "name": "Quiz",
                "created_at": "2026-02-01",
                "assignment_objectives": [],
            },
        ]
        html, status, _reads = self._render_batch(
            enrollments, grades, assignments, "?print=1&search=Quinn&student_id=s-muted",
        )
        self.assertEqual(status, 200)
        self.assertLess(html.find("Adams, Amy"), html.find("Baker, Bo"))
        self.assertLess(html.find("Baker, Bo"), html.find("Zebra, Zoe"))
        self.assertNotIn("Quinn, Quin", html)
        self.assertNotIn(">X<", html)
        self.assertNotIn(">P<", html)
        adams = html.split("Adams, Amy", 1)[1].split("Baker, Bo", 1)[0]
        baker = html.split("Baker, Bo", 1)[1].split("Zebra, Zoe", 1)[0]
        adams_names = re.findall(
            r'class="assignment-name">\s*([^<]+?)\s*</td>',
            adams,
        )
        adams_scores = re.findall(
            r'class="grade-cell">\s*([^<]*?)\s*</td>',
            adams,
        )
        self.assertEqual(adams_names, ["Quiz", "Quiz"])
        self.assertEqual([score.strip() for score in adams_scores], ["M", "R"])
        self.assertIn("Student history report", baker)
        self.assertIn("Test Upload", baker)
        self.assertIn(">Assignment<", baker)
        self.assertNotIn("assignment-name", baker)

    def test_batch_history_read_count_matches_for_3_and_30_students(self):
        def enrollments(count):
            return [
                {
                    "muted": False,
                    "profiles": {
                        "id": f"s{index:02d}",
                        "full_name": f"Student, {index:02d}",
                        "email": "",
                    },
                }
                for index in range(count)
            ]

        _html3, status3, reads3 = self._render_batch(enrollments(3), [], [], "")
        _html30, status30, reads30 = self._render_batch(enrollments(30), [], [], "")
        self.assertEqual(status3, 200)
        self.assertEqual(status30, 200)
        self.assertEqual(reads3, reads30)
        self.assertEqual(reads3, 7)

    def test_packet_grades_use_one_read_per_hundred_objectives(self):
        from unittest.mock import MagicMock, patch
        from app.helpers.history_packet import _history_packet_grades

        chain = MagicMock()
        chain.select.return_value = chain
        chain.in_.return_value = chain
        chain.order.return_value = chain
        chain.range.return_value = chain
        chain.execute.return_value = MagicMock(data=[])
        admin = MagicMock()
        admin.table.return_value = chain
        with patch("app.helpers.history_packet.supabase_admin", admin):
            rows = _history_packet_grades([f"lo{index}" for index in range(101)])
        self.assertEqual(rows, [])
        self.assertEqual(chain.execute.call_count, 2)
        chain.select.assert_called_with(
            "student_id, learning_objective_id, assignment_id, top_score, second_score"
        )
        self.assertEqual(chain.in_.call_args_list[0][0][0], "learning_objective_id")
        self.assertEqual(len(chain.in_.call_args_list[0][0][1]), 100)
        self.assertEqual(len(chain.in_.call_args_list[1][0][1]), 1)

    def test_batch_history_requires_login_and_rejects_other_owner(self):
        from flask import session
        from unittest.mock import patch
        from app.routes.reports import student_history_print

        app = create_app()
        client = app.test_client()
        signed_out = client.get("/class/c1/student-history/print")
        self.assertEqual(signed_out.status_code, 302)
        self.assertTrue(signed_out.headers["Location"].endswith("/login"))
        with app.test_request_context("/class/c1/student-history/print"):
            session["user_id"] = "prof-1"
            session["role"] = "instructor"
            with patch("app.routes.reports._instructor_owns_class", return_value=False):
                denied = student_history_print("c1")
        self.assertEqual(denied, ("Forbidden", 403))

    def test_batch_grades_read_failure_returns_500_without_sheets(self):
        enrollments = [
            {"muted": False, "profiles": {"id": "s-adams", "full_name": "Adams, Amy", "email": ""}},
        ]
        body, status, _reads = self._render_batch(
            enrollments, [], [], "", fail="grades",
        )
        self.assertEqual(status, 500)
        self.assertIn("Could not load student history. Please try again.", body)
        self.assertNotIn("history-batch-student", body)
        self.assertNotIn("Adams, Amy", body)

    def test_batch_enrollments_read_failure_returns_500(self):
        body, status, _reads = self._render_batch([], [], [], "", fail="enrollments")
        self.assertEqual(status, 500)
        self.assertIn("Could not load student history. Please try again.", body)
        self.assertNotIn("history-batch-student", body)

    def test_batch_template_reuses_table_and_print_rules(self):
        root = os.path.join(os.path.dirname(__file__), "..", "..", "app", "templates")
        with open(os.path.join(root, "classes", "student_history_print.html"), encoding="utf-8") as handle:
            batch = handle.read()
        with open(os.path.join(root, "classes", "student_history.html"), encoding="utf-8") as handle:
            page = handle.read()
        with open(os.path.join(root, "classes", "student_history_table.html"), encoding="utf-8") as handle:
            table = handle.read()
        self.assertIn("@page { size: letter portrait; margin: 0; }", batch)
        self.assertIn("print-color-adjust: exact", batch)
        self.assertIn("break-after: page", batch)
        self.assertIn("break-inside: avoid", batch)
        self.assertIn("data-auto-print", batch)
        script_path = os.path.join(root, "..", "static", "js", "app_shell.js")
        with open(script_path, encoding="utf-8") as handle:
            print_script = handle.read()
        self.assertIn("}, 250);", print_script)
        self.assertIn('{% include "classes/student_history_table.html" %}', page)
        self.assertIn('{% include "classes/student_history_table.html" %}', batch)
        self.assertLess(table.find("<thead>"), table.find("history-sheet-name"))
        self.assertLess(table.find("history-sheet-name"), table.find("</thead>"))
        self.assertIn("history-print-spacer", table)
        self.assertIn('class="history-col-assignment"', table)
        self.assertIn("{% for cell in row.cells %}", table)
        self.assertNotIn("namespace(", table)

    def test_history_table_css_lives_in_one_file(self):
        root = os.path.join(os.path.dirname(__file__), "..", "..", "app")
        with open(os.path.join(root, "static", "css", "student_history_table.css"), encoding="utf-8") as handle:
            css = handle.read()
        with open(os.path.join(root, "templates", "classes", "student_history.html"), encoding="utf-8") as handle:
            page = handle.read()
        with open(os.path.join(root, "templates", "classes", "student_history_print.html"), encoding="utf-8") as handle:
            batch = handle.read()
        self.assertEqual(css.count(".history-col-assignment"), 1)
        self.assertNotIn(".history-col-assignment", page)
        self.assertNotIn(".history-col-assignment", batch)
        self.assertIn("table-layout: fixed", css)
        self.assertNotIn("table-layout: fixed", page)
        self.assertNotIn("table-layout: fixed", batch)
        self.assertEqual(page.count("student_history_table.css"), 1)
        self.assertEqual(batch.count("student_history_table.css"), 1)

    def test_batch_route_does_not_read_search(self):
        import inspect
        from app.routes.reports import student_history_print

        source = inspect.getsource(inspect.unwrap(student_history_print))
        self.assertNotIn("request.args", source)

    def test_batch_and_single_pages_render_the_same_table(self):
        from flask import session
        from unittest.mock import patch
        from app.routes.reports import student_history, student_history_print

        objectives = [
            {"id": "lo-10", "name": "Ten", "vendor_code": "AO10", "description": "Ten", "required_ms": 1},
            {"id": "lo-2", "name": "Two", "vendor_code": "AO2", "description": "Two", "required_ms": 1},
            {"id": "lo-z", "name": "Zed", "vendor_code": "ZZ", "description": "Zed", "required_ms": 1},
        ]
        enrollments = [{
            "id": "e1",
            "class_id": "c1",
            "student_id": "s-adams",
            "muted": False,
            "profiles": {
                "id": "s-adams",
                "full_name": "Adams, Amy",
                "role": "student",
                "email": "a@b.edu",
            },
        }]
        grades = [
            {
                "student_id": "s-adams",
                "learning_objective_id": "lo-10",
                "assignment_id": "a1",
                "top_score": "R",
                "second_score": None,
                "assignments": {"name": "Quiz"},
            },
            {
                "student_id": "s-adams",
                "learning_objective_id": "lo-2",
                "assignment_id": "a1",
                "top_score": "M",
                "second_score": "P",
                "assignments": {"name": "Quiz"},
            },
            {
                "student_id": "s-adams",
                "learning_objective_id": "lo-z",
                "assignment_id": "a1",
                "top_score": "X",
                "second_score": None,
                "assignments": {"name": "Quiz"},
            },
        ]
        assignments = [{
            "id": "a1",
            "name": "Quiz",
            "created_at": "2026-01-01",
            "assignment_objectives": [],
        }]
        reads = {"n": 0}
        admin = self._batch_supabase(reads, enrollments, grades, assignments, objectives)
        app = create_app()
        with app.test_request_context("/class/c1/student/s-adams/history"):
            session["user_id"] = "prof-1"
            session["role"] = "instructor"
            with patch("app.routes.reports.supabase_admin", admin), \
                 patch("app.helpers.class_progress.supabase_admin", admin), \
                 patch("app.helpers.class_data.supabase_admin", admin), \
                 patch("app.helpers.class_access.supabase_admin", admin), \
                 patch("app.models.supabase_admin", admin):
                single = student_history("c1", "s-adams")
        with app.test_request_context("/class/c1/student-history/print"):
            session["user_id"] = "prof-1"
            session["role"] = "instructor"
            with patch("app.routes.reports.supabase_admin", admin), \
                 patch("app.helpers.class_progress.supabase_admin", admin), \
                 patch("app.helpers.history_packet.supabase_admin", admin), \
                 patch("app.helpers.class_data.supabase_admin", admin), \
                 patch("app.helpers.class_access.supabase_admin", admin), \
                 patch("app.models.supabase_admin", admin):
                batch = student_history_print("c1")
        single_table = self._print_table(single)
        batch_table = self._print_table(batch)
        self.assertEqual(single_table, batch_table)
        self.assertIn(">AO2<", single_table)
        self.assertIn(">AO10<", single_table)
        self.assertLess(single_table.find(">AO2<"), single_table.find(">AO10<"))
        self.assertNotIn(">ZZ<", single_table)
        self.assertIn(">M, P<", single_table)
        self.assertIn(">R<", single_table)

    def test_history_badge_whitelist_includes_q_and_excludes_z(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "templates", "classes", "student_history.html",
        )
        with open(path, encoding="utf-8") as handle:
            html = handle.read()
        top = "score1 in ['M', 'MR', 'R', 'Q', 'RQ', 'P', 'X', 'A']"
        second = "score2 in ['M', 'MR', 'R', 'Q', 'RQ', 'P', 'X', 'A']"
        top_at = html.find(top)
        self.assertGreater(top_at, -1)
        second_at = html.find(second)
        self.assertGreater(second_at, -1)
        self.assertGreater(second_at, top_at)
        self.assertIn("'RQ'", html)
        self.assertNotIn("'Z'", html)
