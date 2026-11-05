import unittest
import unittest.mock
from datetime import date

from app import create_app
from tests.routes.fakes import _class_detail_client


class TestProtectedRoutes(unittest.TestCase):
    """Verify that protected pages redirect unauthenticated users."""

    def setUp(self):
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def test_dashboard_requires_auth(self):
        response = self.client.get('/instructor/dashboard')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))

    def test_class_detail_requires_auth(self):
        response = self.client.get('/class/fake-id')
        self.assertIn(response.status_code, (302, 404))


class TestLearningObjectivesProgressColumn(unittest.TestCase):
    def _pool(self, count):
        return [
            {
                "id": "lo-%s" % index,
                "name": "LO %s" % index,
                "vendor_code": "T%s" % index,
                "required_ms": 2,
            }
            for index in range(count)
        ]

    def _passing(self, lo_id):
        return [
            {"learning_objective_id": lo_id, "top_score": "M", "counts_for_mastery": True},
            {"learning_objective_id": lo_id, "top_score": "M", "counts_for_mastery": True},
        ]

    def _class_data(self, pool, grades):
        return {
            "name": "MW",
            "learning_objectives": pool,
            "enrollments": [{
                "muted": False,
                "profiles": {
                    "id": "s1",
                    "full_name": "Lee, Ana",
                    "email": "a@b.edu",
                    "grades": grades,
                },
            }],
        }

    def _render(self, pool, grades):
        from flask import session
        from unittest.mock import patch
        from app.routes.classes import class_learning_objectives_summary
        app = create_app()
        with app.test_request_context("/class/c1/learning-objectives"):
            session["user_id"] = "u1"
            session["role"] = "instructor"
            with patch("app.routes.classes._instructor_owns_class", return_value=True), \
                 patch("app.models.Course.get_full_class_data", return_value=self._class_data(pool, grades)):
                return class_learning_objectives_summary("c1")

    def _row_cell_count(self, html, tag):
        import re
        match = re.search(r"<%s>(.*?)</%s>" % (tag, tag), html, re.S)
        row = re.search(r"<tr\b[^>]*>(.*?)</tr>", match.group(1), re.S)
        return len(re.findall(r"<t[dh]\b", row.group(1)))

    def test_three_of_four_shows_count(self):
        grades = []
        for index in range(3):
            grades.extend(self._passing("lo-%s" % index))
        html = self._render(self._pool(4), grades)
        self.assertIn("3/4", html)
        self.assertNotIn("progress-bar-wrapper", html)

    def test_no_grades_shows_zero_of_n(self):
        html = self._render(self._pool(4), [])
        self.assertIn("0/4", html)
        self.assertNotIn("progress-bar-wrapper", html)

    def test_empty_pool_shows_zero_of_zero(self):
        html = self._render([], [])
        self.assertIn("0/0", html)
        self.assertNotIn("progress-bar-wrapper", html)

    def test_header_rows_and_footer_have_equal_cells(self):
        grades = []
        for index in range(3):
            grades.extend(self._passing("lo-%s" % index))
        html = self._render(self._pool(4), grades)
        header = self._row_cell_count(html, "thead")
        body = self._row_cell_count(html, "tbody")
        footer = self._row_cell_count(html, "tfoot")
        self.assertEqual(header, body)
        self.assertEqual(body, footer)
        self.assertEqual(header, 6)


class TestLoCellStatus(unittest.TestCase):
    def test_status_bands(self):
        from app.helpers.lo_progress import lo_cell_status
        self.assertEqual(lo_cell_status(3, 3), "passed")
        self.assertEqual(lo_cell_status(4, 3), "passed")
        self.assertEqual(lo_cell_status(2, 3), "close")
        self.assertEqual(lo_cell_status(1, 2), "close")
        self.assertEqual(lo_cell_status(0, 1), "behind")
        self.assertEqual(lo_cell_status(0, 2), "behind")
        self.assertEqual(lo_cell_status(1, 3), "behind")

    def test_summary_renders_check_and_yellow_fraction(self):
        from flask import session
        from unittest.mock import patch
        from app.routes.classes import class_learning_objectives_summary
        import re
        pool = [
            {"id": "lo-0", "name": "T0", "vendor_code": "T0", "required_ms": 2},
            {"id": "lo-1", "name": "T1", "vendor_code": "T1", "required_ms": 3},
        ]
        grades = [
            {"learning_objective_id": "lo-0", "top_score": "M", "counts_for_mastery": True},
            {"learning_objective_id": "lo-0", "top_score": "M", "counts_for_mastery": True},
            {"learning_objective_id": "lo-1", "top_score": "M", "counts_for_mastery": True},
            {"learning_objective_id": "lo-1", "top_score": "M", "counts_for_mastery": True},
        ]
        class_data = {
            "name": "MW",
            "learning_objectives": pool,
            "enrollments": [{
                "muted": False,
                "profiles": {
                    "id": "s1",
                    "full_name": "Lee, Ana",
                    "email": "a@b.edu",
                    "grades": grades,
                },
            }],
        }
        app = create_app()
        with app.test_request_context("/class/c1/learning-objectives"):
            session["user_id"] = "u1"
            session["role"] = "instructor"
            with patch("app.routes.classes._instructor_owns_class", return_value=True), \
                 patch("app.models.Course.get_full_class_data", return_value=class_data):
                html = class_learning_objectives_summary("c1")
        self.assertIn(">passed</span>", html)
        self.assertRegex(html, r"text-yellow-700[^>]*>\s*2/3")
        self.assertIsNotNone(re.search(r"text-green-700[\s\S]*?✓", html))
        self.assertIn('title="2/2"', html)
        self.assertIn('title="2/3"', html)

    def test_helper_text_matches_the_page(self):
        from flask import session
        from unittest.mock import patch
        from app.routes.classes import class_learning_objectives_summary
        from app.helpers.lo_progress import (
            learning_objective_cell_text,
            learning_objective_progress_text,
        )
        pool = [
            {"id": "lo-0", "name": "T0", "vendor_code": "T0", "required_ms": 2},
            {"id": "lo-1", "name": "T1", "vendor_code": "T1", "required_ms": 3},
        ]
        grades = [
            {"learning_objective_id": "lo-0", "top_score": "M", "counts_for_mastery": True},
            {"learning_objective_id": "lo-0", "top_score": "M", "counts_for_mastery": True},
            {"learning_objective_id": "lo-1", "top_score": "M", "counts_for_mastery": True},
            {"learning_objective_id": "lo-1", "top_score": "M", "counts_for_mastery": True},
        ]
        class_data = {
            "name": "MW",
            "learning_objectives": pool,
            "enrollments": [{
                "muted": False,
                "profiles": {
                    "id": "s1",
                    "full_name": "Lee, Ana",
                    "email": "a@b.edu",
                    "grades": grades,
                },
            }],
        }
        app = create_app()
        with app.test_request_context("/class/c1/learning-objectives"):
            session["user_id"] = "u1"
            session["role"] = "instructor"
            with patch("app.routes.classes._instructor_owns_class", return_value=True), \
                 patch("app.models.Course.get_full_class_data", return_value=class_data):
                html = class_learning_objectives_summary("c1")
        self.assertEqual(learning_objective_cell_text(2, 2), "✓")
        self.assertEqual(learning_objective_cell_text(2, 3), "2/3")
        self.assertEqual(learning_objective_progress_text(1, 2), "1/2")
        self.assertIn(learning_objective_cell_text(2, 2), html)
        self.assertIn(learning_objective_cell_text(2, 3), html)
        self.assertIn(learning_objective_progress_text(1, 2), html)


class TestClassSectionNumber(unittest.TestCase):
    def _capture_insert(self):
        from unittest.mock import MagicMock
        saved = {}

        def insert(payload):
            saved["payload"] = payload
            result = MagicMock()
            result.data = [{"id": "new-class"}]
            chain = MagicMock()
            chain.execute.return_value = result
            return chain

        return saved, insert

    def _add(self, section, token, days="MW"):
        from flask import session
        from unittest.mock import patch
        from app.routes.classes import add_class
        saved, insert = self._capture_insert()
        app = create_app()
        data = {
            "name": "Test Upload",
            "semester": "Fall",
            "year": "2026",
            "days": days,
            "create_class_token": token,
            "section_number": section,
        }
        with app.test_request_context("/add_class", method="POST", data=data):
            session["user_id"] = "u1"
            session["role"] = "instructor"
            session["create_class_token"] = token
            with patch("app.routes.classes.supabase_admin") as sb:
                sb.table.return_value.insert.side_effect = insert
                result = add_class()
        return result, saved.get("payload")

    def _copy(self, section, token, days="MW"):
        from flask import session
        from unittest.mock import MagicMock, patch
        from app.routes.classes import copy_class
        saved, insert = self._capture_insert()
        source = MagicMock()
        source.data = [{
            "semester": "Fall 2026",
            "section_number": "OLD",
            "num_learning_objectives": 36,
            "min_masteries": 2,
            "is_online": False,
            "auto_convert_m": True,
        }]
        classes = MagicMock()
        classes.select.return_value.eq.return_value.eq.return_value.execute.return_value = source
        classes.insert.side_effect = insert
        lo_resp = MagicMock()
        lo_resp.data = []
        app = create_app()
        data = {
            "name": "Copy of Test",
            "days": days,
            "section_number": section,
            "copy_class_token": token,
        }
        with app.test_request_context("/class/c1/copy", method="POST", data=data):
            session["user_id"] = "u1"
            session["role"] = "instructor"
            session["copy_class_token"] = token
            with patch("app.routes.classes._instructor_owns_class", return_value=True), \
                 patch("app.routes.classes._select_class_pool_learning_objectives", return_value=lo_resp), \
                 patch("app.routes.classes.supabase_admin") as sb:
                sb.table.return_value = classes
                result = copy_class("c1")
        return result, saved.get("payload")

    def test_blank_section_stores_null(self):
        _result, payload = self._add("   ", "section-blank")
        self.assertIsNone(payload["section_number"])
        self.assertNotIn("num_learning_objectives", payload)

    def test_section_keeps_typed_case_after_trim(self):
        _result, payload = self._add(" f26 ", "section-trim")
        self.assertEqual(payload["section_number"], "f26")

    def test_overlong_section_is_rejected(self):
        result, payload = self._add("F" * 33, "section-long")
        self.assertEqual(result[1], 400)
        self.assertIsNone(payload)

    def test_copy_saves_its_own_section(self):
        _result, payload = self._copy(" F26 ", "copy-section")
        self.assertEqual(payload["section_number"], "F26")
        self.assertNotEqual(payload["section_number"], "OLD")
        self.assertNotIn("num_learning_objectives", payload)
        self.assertNotIn("hw_passes_enabled", payload)
        self.assertNotIn("hw_passes_allowed", payload)

    def test_copy_blank_section_stores_null(self):
        _result, payload = self._copy("  ", "copy-blank")
        self.assertIsNone(payload["section_number"])

    def test_copy_overlong_section_returns_400(self):
        result, payload = self._copy("S" * 33, "copy-long")
        self.assertEqual(result[1], 400)
        self.assertIsNone(payload)

    def test_form_prefills_from_values_and_days_list(self):
        from app.helpers.class_form import CLASS_DAYS
        app = create_app()
        values = {
            "name": "Test Upload",
            "semester": "Fall",
            "year": "2026",
            "section_number": "F26",
            "days": "TTH",
            "is_online": True,
            "auto_convert_m": False,
        }
        with app.app_context():
            template = app.jinja_env.get_template("_class_form_fields.html")
            filled = template.module.class_form_fields(values, CLASS_DAYS)
            blank = template.module.class_form_fields(days_options=CLASS_DAYS)
        self.assertIn('value="Fall" selected', filled)
        self.assertIn('value="TTH" selected', filled)
        self.assertIn('name="is_online" value="1" checked', filled)
        self.assertNotIn('name="auto_convert_m" value="1" checked', filled)
        self.assertIn('name="auto_convert_m" value="1" checked', blank)
        for day in CLASS_DAYS:
            self.assertIn('value="%s"' % day, blank)
        self.assertIn('value="Asynchronous"', blank)
        self.assertEqual(CLASS_DAYS, ("MW", "TTH", "MWF", "WF", "Asynchronous"))

    def test_asynchronous_is_accepted_on_create_and_copy(self):
        _created, created = self._add("", "days-async-create", days="Asynchronous")
        _copied, copied = self._copy("", "days-async-copy", days="Asynchronous")
        self.assertEqual(created["days"], "Asynchronous")
        self.assertEqual(copied["days"], "Asynchronous")

    def test_unknown_days_are_rejected(self):
        created, created_payload = self._add("", "days-th-create", days="TH")
        copied, copied_payload = self._copy("", "days-th-copy", days="TH")
        self.assertEqual(created[1], 400)
        self.assertEqual(copied[1], 400)
        self.assertIsNone(created_payload)
        self.assertIsNone(copied_payload)

    def test_blank_days_are_rejected(self):
        created, created_payload = self._add("", "days-blank-create", days="")
        copied, copied_payload = self._copy("", "days-blank-copy", days="")
        self.assertEqual(created[1], 400)
        self.assertEqual(copied[1], 400)
        self.assertIsNone(created_payload)
        self.assertIsNone(copied_payload)

    def test_bad_section_does_not_consume_create_token(self):
        token = "section-resubmit"
        rejected, rejected_payload = self._add("F" * 33, token)
        self.assertEqual(rejected[1], 400)
        self.assertIn("32 characters", rejected[0])
        self.assertIsNone(rejected_payload)
        accepted, accepted_payload = self._add("F26", token)
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(accepted_payload["section_number"], "F26")

    def test_create_schema_error_is_one_insert(self):
        from flask import session
        from unittest.mock import MagicMock, patch
        from app.routes.classes import add_class
        calls = {"n": 0}

        def insert(_payload):
            chain = MagicMock()

            def execute():
                calls["n"] += 1
                raise Exception("Could not find the is_online column of classes in the schema cache")

            chain.execute.side_effect = execute
            return chain

        app = create_app()
        data = {
            "name": "Test Upload",
            "semester": "Fall",
            "year": "2026",
            "days": "MW",
            "create_class_token": "schema-create",
            "section_number": "F26",
        }
        with app.test_request_context("/add_class", method="POST", data=data):
            session["user_id"] = "u1"
            session["role"] = "instructor"
            session["create_class_token"] = "schema-create"
            with patch("app.routes.classes.supabase_admin") as sb:
                sb.table.return_value.insert.side_effect = insert
                result = add_class()
        self.assertEqual(result[1], 500)
        self.assertEqual(result[0], "Failed to create class.")
        self.assertEqual(calls["n"], 1)

    def test_copy_schema_error_is_one_insert(self):
        from flask import session
        from unittest.mock import MagicMock, patch
        from app.routes.classes import copy_class
        calls = {"n": 0}
        saved = {}
        source = MagicMock()
        source.data = [{
            "semester": "Fall 2026",
            "section_number": "OLD",
            "min_masteries": 2,
            "is_online": False,
            "auto_convert_m": True,
            "hw_passes_enabled": True,
            "hw_passes_allowed": 2,
        }]
        classes = MagicMock()
        classes.select.return_value.eq.return_value.eq.return_value.execute.return_value = source

        def insert(payload):
            saved["payload"] = payload
            chain = MagicMock()

            def execute():
                calls["n"] += 1
                raise Exception("Could not find the is_online column of classes in the schema cache")

            chain.execute.side_effect = execute
            return chain

        classes.insert.side_effect = insert
        lo_resp = MagicMock()
        lo_resp.data = []
        app = create_app()
        data = {
            "name": "Copy of Test",
            "days": "MW",
            "section_number": "F26",
            "copy_class_token": "schema-copy",
        }
        with app.test_request_context("/class/c1/copy", method="POST", data=data):
            session["user_id"] = "u1"
            session["role"] = "instructor"
            session["copy_class_token"] = "schema-copy"
            with patch("app.routes.classes._instructor_owns_class", return_value=True), \
                 patch("app.routes.classes._select_class_pool_learning_objectives", return_value=lo_resp), \
                 patch("app.routes.classes.supabase_admin") as sb:
                sb.table.return_value = classes
                result = copy_class("c1")
        self.assertEqual(result[1], 500)
        self.assertEqual(result[0], "Failed to copy class.")
        self.assertEqual(calls["n"], 1)
        self.assertNotIn("hw_passes_enabled", saved["payload"])
        self.assertNotIn("hw_passes_allowed", saved["payload"])

    def test_history_enrollment_select_includes_email_once(self):
        from flask import session
        from unittest.mock import MagicMock, patch
        from app.routes.reports import student_history
        selects = []

        def select(cols):
            selects.append(cols)
            chain = MagicMock()
            chain.eq.return_value = chain
            chain.single.return_value = chain
            chain.execute.side_effect = Exception("Could not find the email column of profiles in the schema cache")
            return chain

        app = create_app()
        with app.test_request_context("/class/c1/student/s1/history"):
            session["user_id"] = "u1"
            session["role"] = "instructor"
            with patch("app.routes.reports._instructor_owns_class", return_value=True), \
                 patch("app.models.Course.get_full_class_data", return_value={"name": "MW", "learning_objectives": []}), \
                 patch("app.routes.reports.supabase_admin") as sb:
                sb.table.return_value.select.side_effect = select
                result = student_history("c1", "s1")
        self.assertEqual(result.status_code, 302)
        self.assertTrue(result.headers["Location"].endswith("/class/c1/reports"))
        self.assertEqual(selects, ["student_id, profiles(id, full_name, email)"])


class TestClassUpdate(unittest.TestCase):
    def _form(self, **overrides):
        data = {
            "name": "Renamed",
            "semester": "Fall",
            "year": "2026",
            "days": "TTH",
            "section_number": "S26",
            "is_online": "1",
            "auto_convert_m": "1",
        }
        data.update(overrides)
        return data

    def _update(self, data, owns=True, execute_error=None):
        from flask import session
        from unittest.mock import MagicMock, patch
        from app.routes.classes import update_class
        saved = {}
        tables = []
        execute_calls = {"n": 0}

        def table(name):
            tables.append(name)
            chain = MagicMock()

            def update(payload):
                saved["payload"] = payload
                return chain

            def execute():
                execute_calls["n"] += 1
                if execute_error is not None:
                    raise execute_error
                return MagicMock(data=[{"id": "c1"}])

            chain.update.side_effect = update
            chain.eq.return_value = chain
            chain.execute.side_effect = execute
            return chain

        app = create_app()
        with app.test_request_context("/class/c1/update", method="POST", data=data):
            session["user_id"] = "u1"
            session["role"] = "instructor"
            with patch("app.routes.classes._instructor_owns_class", return_value=owns), \
                 patch("app.routes.classes.supabase_admin") as sb:
                sb.table.side_effect = table
                result = update_class("c1")
        saved["execute_calls"] = execute_calls["n"]
        return result, saved.get("payload"), tables, saved["execute_calls"]

    def test_non_owner_is_rejected(self):
        result, payload, tables, calls = self._update(self._form(), owns=False)
        self.assertEqual(result[1], 403)
        self.assertEqual(result[0].get_json()["error"], "Forbidden")
        self.assertIsNone(payload)
        self.assertEqual(tables, [])
        self.assertEqual(calls, 0)

    def test_valid_edit_updates_only_class_details(self):
        result, payload, tables, calls = self._update(self._form())
        self.assertEqual(result.status_code, 302)
        self.assertTrue(result.headers["Location"].endswith("/instructor/dashboard"))
        self.assertEqual(payload, {
            "name": "Renamed",
            "semester": "Fall 2026",
            "days": "TTH",
            "section_number": "S26",
            "is_online": True,
            "auto_convert_m": True,
        })
        self.assertEqual(tables, ["classes"])
        self.assertEqual(calls, 1)

    def test_blank_section_stores_null(self):
        _result, payload, tables, _calls = self._update(self._form(section_number="  "))
        self.assertIsNone(payload["section_number"])
        self.assertEqual(tables, ["classes"])

    def test_invalid_days_return_400(self):
        result, payload, tables, calls = self._update(self._form(days="TH"))
        self.assertEqual(result[1], 400)
        self.assertIn("Class days", result[0])
        self.assertIsNone(payload)
        self.assertEqual(tables, [])
        self.assertEqual(calls, 0)

    def test_legacy_days_without_a_new_choice_return_400(self):
        result, payload, tables, calls = self._update(self._form(days=""))
        self.assertEqual(result[1], 400)
        self.assertIn("Class days", result[0])
        self.assertIsNone(payload)
        self.assertEqual(tables, [])
        self.assertEqual(calls, 0)

    def test_schema_error_is_not_retried(self):
        result, payload, tables, calls = self._update(
            self._form(),
            execute_error=Exception("section_number column is missing from the schema cache"),
        )
        self.assertEqual(result[1], 500)
        self.assertEqual(result[0], "Failed to update class.")
        self.assertEqual(tables, ["classes"])
        self.assertEqual(calls, 1)

    def test_card_title_reflects_the_edit(self):
        from flask import render_template, session
        from app.helpers.class_form import CLASS_DAYS, class_display_title
        title = class_display_title("Renamed", "TTH", None)
        self.assertEqual(title, "Renamed | TTH")
        app = create_app()
        with app.test_request_context("/instructor/dashboard"):
            session["role"] = "instructor"
            html = render_template(
                "classes/instructor_select_class.html",
                classes=[{
                    "id": "c1",
                    "name": "Renamed",
                    "days": "TTH",
                    "section_number": None,
                    "semester": "Fall 2026",
                    "is_online": False,
                    "auto_convert_m": False,
                }],
                create_class_token="t",
                copy_class_token="c",
                class_days=CLASS_DAYS,
            )
        self.assertIn(title, html)
        self.assertNotIn("Renamed | TTH |", html)

    def test_edit_json_escapes_quote_and_script(self):
        import json
        import re
        from flask import render_template, session
        from app.helpers.class_form import CLASS_DAYS
        name = 'Algebra "A" </script>'
        classes = [
            {
                "id": "c1",
                "name": name,
                "days": "TH",
                "section_number": "F26",
                "semester": "Fall 2026",
                "is_online": False,
                "auto_convert_m": False,
            },
            {
                "id": "c2",
                "name": "Other",
                "days": "MW",
                "section_number": "A1",
                "semester": "Spring 2026",
                "is_online": True,
                "auto_convert_m": True,
            },
        ]
        app = create_app()
        with app.test_request_context("/instructor/dashboard"):
            session["role"] = "instructor"
            html = render_template(
                "classes/instructor_select_class.html",
                classes=classes,
                create_class_token="t",
                copy_class_token="c",
                class_days=CLASS_DAYS,
            )
        blobs = re.findall(
            r'<script type="application/json" class="class-edit-data">(.*?)</script>',
            html,
            re.S,
        )
        self.assertEqual(len(blobs), len(classes))
        self.assertEqual(html.count('type="application/json"'), len(classes))
        parsed = [json.loads(blob) for blob in blobs]
        dangerous_blob = next(
            blob for blob, item in zip(blobs, parsed) if item["id"] == "c1"
        )
        dangerous = json.loads(dangerous_blob)
        self.assertEqual(dangerous["name"], name)
        self.assertEqual(dangerous["semester"], "Fall")
        self.assertEqual(dangerous["year"], "2026")
        self.assertEqual(dangerous["days"], "TH")
        self.assertNotIn("</script>", dangerous_blob)
        self.assertIn("\\u003c/script\\u003e", dangerous_blob)
        for blob in blobs:
            self.assertNotIn("</script>", blob)


class TestClassDetailReads(unittest.TestCase):
    TODAY = date(2026, 10, 21)
    CLASS_ID = "class-full"
    NONE_ID = "class-none"
    EMPTY_ID = "class-empty"
    NULL_ID = "class-null"
    FULL_CALLS = [
        "classes", "classes", "learning_objectives", "enrollments", "grades",
        "assignment_objectives", "assignments", "grades", "assignments",
        "homework_scores", "revision_reminders_sent",
    ]

    def _class_row(self, class_id, name):
        return {
            "id": class_id, "instructor_id": "inst-1", "name": name,
            "semester": "Fall", "days": "MWF", "section_number": "1",
            "auto_convert_m": False, "is_online": False,
        }

    def _enrollment(self, class_id, student_id, full_name, email, muted=False):
        return {
            "id": "enr-%s" % student_id, "class_id": class_id,
            "student_id": student_id, "muted": muted,
            "profiles": {
                "id": student_id, "full_name": full_name,
                "role": "student", "email": email,
            },
        }

    def _objective(self, lo_id, class_id, vendor_code, name, required_ms):
        return {
            "id": lo_id, "class_id": class_id, "vendor_code": vendor_code,
            "name": name, "description": "%s text" % name,
            "required_ms": required_ms,
        }

    def _assignment(self, assignment_id, class_id, name, homework_group, revision_due, links):
        return {
            "id": assignment_id, "class_id": class_id, "name": name,
            "homework_group": homework_group, "date_returned": "2026-10-17",
            "revision_due": revision_due, "created_at": "2026-10-01T00:00:00",
            "assignment_type": "mastery_opp", "assignment_objectives": links,
        }

    def _link(self, assignment_id, lo_id, vendor_code, class_id):
        return {
            "assignment_id": assignment_id,
            "learning_objective_id": lo_id,
            "assignments": {"class_id": class_id},
            "learning_objectives": {
                "id": lo_id, "vendor_code": vendor_code,
                "description": "%s text" % vendor_code,
            },
        }

    def _grade(self, grade_id, student_id, lo_id, top_score, assignment, lo_name, lo_code):
        row = {
            "id": grade_id, "student_id": student_id,
            "learning_objective_id": lo_id, "top_score": top_score,
            "second_score": None, "counts_for_mastery": True,
            "assignment_id": None if assignment is None else assignment["id"],
            "hw_score_at_entry": None,
            "learning_objectives": {
                "id": lo_id, "name": lo_name, "vendor_code": lo_code,
            },
        }
        if assignment is not None:
            row["assignments"] = {
                "id": assignment["id"], "name": assignment["name"],
                "revision_due": assignment["revision_due"],
                "homework_group": assignment["homework_group"],
                "class_id": assignment["class_id"],
            }
        return row

    def _full_store(self, reminded=False):
        live = self._assignment("asg-live", self.CLASS_ID, "Quiz Live", "HW 1", "2026-10-24", [
            self._link("asg-live", "lo-2", "A1", self.CLASS_ID)])
        sibling = self._assignment("asg-sib", self.CLASS_ID, "Quiz Sibling", "HW 1", "2026-10-24", [])
        exam = self._assignment("asg-rq", self.CLASS_ID, "Exam RQ", "", "2026-10-20", [
            self._link("asg-rq", "lo-1", "B2", self.CLASS_ID)])
        passed = self._assignment("asg-pass", self.CLASS_ID, "Free Pass Quiz", "", "2026-10-24", [])
        other = self._assignment("asg-other", "class-other", "Other Class Quiz", "HW 1", "2026-10-24", [])
        mastery = self._assignment("asg-m", self.CLASS_ID, "Mastery Check", "", "2026-11-01", [
            self._link("asg-m", "lo-1", "B2", self.CLASS_ID)])
        una = self._assignment("asg-una", self.CLASS_ID, "Unattached Quiz", "", "2026-10-21", [])
        ada_r = self._grade("g-ada-r", "stu-ada", "lo-2", "R", live, "Alpha", "A1")
        bea_rq = self._grade("g-bea-rq", "stu-bea", "lo-1", "RQ", exam, "Beta", "B2")
        bea_rq["hw_score_at_entry"] = 80
        mute_r = self._grade("g-mute-r", "stu-mute", "lo-2", "R", live, "Alpha", "A1")
        mute_r["hw_score_at_entry"] = 90
        out_r = self._grade("g-out-r", "stu-out", "lo-2", "R", live, "Alpha", "A1")
        out_r["hw_score_at_entry"] = 90
        ott_r = self._grade("g-ott-r", "stu-ott", "lo-2", "R", other, "Alpha", "A1")
        pass_r = self._grade("g-pass-r", "stu-pass", "lo-2", "R", passed, "Alpha", "A1")
        pass_r["hw_score_at_entry"] = -1
        null_r = self._grade("g-null-r", "stu-null", "lo-2", "R", None, "Alpha", "A1")
        null_r["hw_score_at_entry"] = 90
        una_r = self._grade("g-una-r", "stu-una", "lo-9", "R", una, "Gamma", "C3")
        una_r["hw_score_at_entry"] = 90
        reminders = []
        if reminded:
            reminders.append({
                "class_id": self.CLASS_ID, "student_id": "stu-ada",
                "assignment_id": "asg-live", "learning_objective_id": "lo-2",
                "revision_due": "2026-10-24",
            })
        return {
            "classes": [self._class_row(self.CLASS_ID, "Algebra")],
            "learning_objectives": [
                self._objective("lo-1", self.CLASS_ID, "B2", "Beta", 2),
                self._objective("lo-2", self.CLASS_ID, "A1", "Alpha", 1),
                self._objective("lo-9", self.CLASS_ID, "C3", "Gamma", 2),
                self._objective("lo-out", "class-other", "Z9", "Outside", 1),
            ],
            "enrollments": [
                self._enrollment(self.CLASS_ID, "stu-ada", "Ada, Ann", "ada@example.edu"),
                self._enrollment(self.CLASS_ID, "stu-bea", "Bea, Bo", "bea@example.edu"),
                self._enrollment(self.CLASS_ID, "stu-mute", "Mute, Mo", "mo@example.edu", muted=True),
                self._enrollment(self.CLASS_ID, "stu-ott", "Ott, Oz", "ott@example.edu"),
                self._enrollment(self.CLASS_ID, "stu-pass", "Pass, Pat", "pat@example.edu"),
                self._enrollment(self.CLASS_ID, "stu-null", "Null, Ned", "ned@example.edu"),
                self._enrollment(self.CLASS_ID, "stu-una", "Una, Jo", "una@example.edu"),
            ],
            "assignment_objectives": [
                self._link("asg-live", "lo-2", "A1", self.CLASS_ID),
                self._link("asg-rq", "lo-1", "B2", self.CLASS_ID),
            ],
            "assignments": [live, sibling, exam, passed, other, mastery, una],
            "grades": [
                self._grade("g-ada-m-a", "stu-ada", "lo-2", "M", mastery, "Alpha", "A1"),
                ada_r,
                self._grade("g-ada-m-b", "stu-ada", "lo-1", "M", mastery, "Beta", "B2"),
                self._grade("g-ada-free", "stu-ada", "lo-9", "M", mastery, "Gamma", "C3"),
                self._grade("g-ada-out", "stu-ada", "lo-out", "M", mastery, "Outside", "Z9"),
                bea_rq,
                self._grade("g-bea-m", "stu-bea", "lo-1", "M", mastery, "Beta", "B2"),
                mute_r, out_r, ott_r,
                self._grade("g-ott-m", "stu-ott", "lo-1", "M", mastery, "Beta", "B2"),
                pass_r, null_r, una_r,
            ],
            "homework_scores": [
                {"id": "hs-sib", "class_id": self.CLASS_ID, "student_id": "stu-ada", "homework_group": "asg-sib", "score_pct": 10},
                {"id": "hs-group", "class_id": self.CLASS_ID, "student_id": "stu-ada", "homework_group": "HW 1", "score_pct": 80},
                {"id": "hs-ott", "class_id": self.CLASS_ID, "student_id": "stu-ott", "homework_group": "HW 1", "score_pct": 95},
                {"id": "hs-pass", "class_id": self.CLASS_ID, "student_id": "stu-pass", "homework_group": "asg-pass", "score_pct": 90},
            ],
            "revision_reminders_sent": reminders,
        }

    def _page(self, store, class_id):
        from flask import template_rendered
        from app import models
        from app.routes import classes as r
        calls, selects = [], []
        admin = _class_detail_client(store, calls, selects)
        app = create_app()
        app.config["TESTING"] = True
        captured = {}

        def grab(sender, template, context, **kwargs):
            if getattr(template, "name", "") == "classes/class_detail.html":
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
                    unittest.mock.patch("app.helpers.class_access.supabase_admin", admin), \
                    unittest.mock.patch("app.helpers.revision_mail.supabase_admin", admin), \
                    unittest.mock.patch.object(r, "local_today", return_value=self.TODAY):
                response = client.get("/class/%s" % class_id)
        finally:
            template_rendered.disconnect(grab, app)
        return response, calls, captured.get("context"), selects

    def _cells(self, student):
        return [{
            "id": item["learning_objective_id"],
            "vendor_code": item["vendor_code"],
            "required_ms": item["required_ms"],
            "m_count": item["m_count"],
            "is_passed": item["is_passed"],
        } for item in student.get("learning_objectives") or []]

    def _by_id(self, students):
        return {student["id"]: student for student in students}

    def _revision_rows(self, report):
        rows = []
        for view in report["views"]:
            for student in view["students"]:
                for assignment in student["assignments"]:
                    for obj in assignment["objectives"]:
                        rows.append((
                            view["key"], student["student_name"],
                            assignment["assignment_name"], assignment["revision_due"],
                            obj["lo_code"], obj["lo_name"], obj["top_score"],
                        ))
        return rows

    def _item_html(self, page, name):
        marker = ">%s</span>" % name
        start = page.find(marker)
        self.assertGreaterEqual(start, 0)
        stop = page.find('class="student-item', start)
        if stop < 0:
            stop = start + 800
        return page[start:stop]

    def test_full_class_pins_revisions_mastery_and_call_order(self):
        response, calls, context, _selects = self._page(self._full_store(), self.CLASS_ID)
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, self.FULL_CALLS)
        self.assertNotIn("all_los", context)
        self.assertEqual(
            [(item["id"], item["name"]) for item in context["learning_objectives"]],
            [("lo-2", "Alpha"), ("lo-1", "Beta")],
        )
        active = self._by_id(context["students"])
        everyone = self._by_id(context["all_students"])
        self.assertEqual(
            [student["id"] for student in context["students"]],
            ["stu-ada", "stu-bea", "stu-null", "stu-ott", "stu-pass", "stu-una"],
        )
        self.assertNotIn("stu-mute", active)
        self.assertNotIn("stu-out", everyone)
        self.assertEqual(active["stu-ada"]["objective_total"], 2)
        self.assertEqual(self._cells(active["stu-ada"]), [
            {"id": "lo-2", "vendor_code": "A1", "required_ms": 1, "m_count": 1, "is_passed": True},
            {"id": "lo-1", "vendor_code": "B2", "required_ms": 2, "m_count": 1, "is_passed": False},
        ])
        self.assertEqual(active["stu-bea"]["objective_total"], 2)
        self.assertEqual(self._cells(active["stu-bea"]), [
            {"id": "lo-1", "vendor_code": "B2", "required_ms": 2, "m_count": 1, "is_passed": False},
        ])
        self.assertEqual(active["stu-una"]["objective_total"], 2)
        self.assertEqual(self._cells(active["stu-una"]), [])
        self.assertEqual(context["revision_report"]["overdue_revision_count"], 1)
        self.assertEqual(self._revision_rows(context["revision_report"]), [
            ("overdue", "Bea, Bo", "Exam RQ", "2026-10-20", "B2", "Beta", "RQ"),
            ("today", "Una, Jo", "Unattached Quiz", "2026-10-21", "C3", "Gamma", "R"),
            ("3", "Ada, Ann", "Quiz Live", "2026-10-24", "A1", "Alpha", "R"),
        ])
        self.assertEqual(context["revision_unsent_count"], 1)
        self.assertEqual(context["revision_email_note"], "")
        self.assertIn("Algebra | MWF | 1", page)
        self.assertIn('id="activeStudentsCount" class="text-3xl font-semibold text-slate-900 dark:text-white">6</p>', page)
        self.assertIn("6 active students (7 total)", page)
        self.assertIn('text-orange-400">1</p>', page)
        self.assertIn("Overdue (1)", page)
        self.assertIn("Due today (1)", page)
        self.assertIn("Due 2026-10-20", page)
        self.assertIn("Due 2026-10-21", page)
        self.assertIn("Unattached Quiz", page)
        self.assertIn("Due in 3 days ★ (1 not emailed)", page)
        self.assertIn("Due 2026-10-24", page)
        self.assertIn("Quiz Live", page)
        self.assertIn("Exam RQ", page)
        self.assertIn("Printed 2026-10-21", page)
        self.assertIn(">50%</span>", self._item_html(page, "Ada, Ann"))
        self.assertIn(">0%</span>", self._item_html(page, "Bea, Bo"))
        self.assertIn(">0%</span>", self._item_html(page, "Una, Jo"))
        self.assertIn('line-through">Mute, Mo', page)
        self.assertNotIn("Outsider, Sid", page)
        revision_html = page.split('id="overdueRevisionsModal"', 1)[1].split(
            'id="dashboardAllStudentsJson"', 1
        )[0]
        self.assertIn("Quiz Live", revision_html)
        self.assertIn("Unattached Quiz", revision_html)
        self.assertIn("Exam RQ", revision_html)
        self.assertNotIn("Other Class Quiz", revision_html)
        self.assertNotIn("All students have been emailed.", page)
        self.assertNotIn("No students have a usable email address.", page)

    def test_reminded_student_sets_the_email_note(self):
        response, calls, context, _selects = self._page(self._full_store(reminded=True), self.CLASS_ID)
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, self.FULL_CALLS)
        self.assertEqual(context["revision_unsent_count"], 0)
        self.assertEqual(context["revision_email_note"], "All students have been emailed.")
        self.assertIn("All students have been emailed.", page)
        self.assertNotIn("not emailed", page)

    def test_class_with_no_objectives_skips_revision_grade_reads(self):
        store = {
            "classes": [self._class_row(self.NONE_ID, "Empty")],
            "learning_objectives": [],
            "enrollments": [self._enrollment(self.NONE_ID, "stu-zoe", "Zoe, Ann", "zoe@example.edu")],
            "assignment_objectives": [], "assignments": [], "grades": [],
            "homework_scores": [], "revision_reminders_sent": [],
        }
        response, calls, context, _selects = self._page(store, self.NONE_ID)
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [
            "classes", "classes", "learning_objectives", "enrollments",
            "assignments", "revision_reminders_sent",
        ])
        student = context["students"][0]
        self.assertEqual(student["objective_total"], 0)
        self.assertEqual(student["learning_objectives"], [])
        self.assertEqual(context["learning_objectives"], [])
        self.assertEqual(context["revision_report"]["overdue_revision_count"], 0)
        self.assertEqual(context["revision_unsent_count"], 0)
        self.assertEqual(context["revision_email_note"], "")
        self.assertIn(">0%</span>", self._item_html(page, "Zoe, Ann"))
        self.assertNotIn("homework_scores", calls)
        self.assertEqual(calls.count("grades"), 0)

    def test_class_with_no_grades_still_reads_grades_and_skips_homework(self):
        store = {
            "classes": [self._class_row(self.EMPTY_ID, "No Grades")],
            "learning_objectives": [self._objective("lo-2", self.EMPTY_ID, "A1", "Alpha", 1)],
            "enrollments": [self._enrollment(self.EMPTY_ID, "stu-ada", "Ada, Ann", "ada@example.edu")],
            "assignment_objectives": [self._link("asg-live", "lo-2", "A1", self.EMPTY_ID)],
            "assignments": [self._assignment("asg-live", self.EMPTY_ID, "Quiz Live", "HW 1", "2026-10-24", [
                self._link("asg-live", "lo-2", "A1", self.EMPTY_ID)])],
            "grades": [], "homework_scores": [], "revision_reminders_sent": [],
        }
        response, calls, context, _selects = self._page(store, self.EMPTY_ID)
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [
            "classes", "classes", "learning_objectives", "enrollments", "grades",
            "assignment_objectives", "assignments", "grades",
            "revision_reminders_sent",
        ])
        student = context["students"][0]
        self.assertEqual(student["objective_total"], 1)
        self.assertEqual(student["learning_objectives"], [])
        self.assertEqual(
            [(item["id"], item["name"]) for item in context["learning_objectives"]],
            [("lo-2", "Alpha")],
        )
        self.assertEqual(self._revision_rows(context["revision_report"]), [])
        self.assertEqual(context["revision_unsent_count"], 0)
        self.assertEqual(context["revision_email_note"], "")
        self.assertIn(">0%</span>", self._item_html(page, "Ada, Ann"))
        self.assertNotIn("homework_scores", calls)

    def test_null_assignment_id_is_left_out_of_the_report(self):
        store = {
            "classes": [self._class_row(self.NULL_ID, "Null Only")],
            "learning_objectives": [self._objective("lo-2", self.NULL_ID, "A1", "Alpha", 1)],
            "enrollments": [self._enrollment(self.NULL_ID, "stu-null", "Null, Ned", "ned@example.edu")],
            "assignment_objectives": [self._link("asg-live", "lo-2", "A1", self.NULL_ID)],
            "assignments": [],
            "grades": [self._grade("g-null-r", "stu-null", "lo-2", "R", None, "Alpha", "A1")],
            "homework_scores": [{
                "id": "hs-null", "class_id": self.NULL_ID, "student_id": "stu-null",
                "homework_group": "HW 1", "score_pct": 95,
            }],
            "revision_reminders_sent": [],
        }
        store["grades"][0]["hw_score_at_entry"] = 90
        response, calls, context, _selects = self._page(store, self.NULL_ID)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [
            "classes", "classes", "learning_objectives", "enrollments", "grades",
            "assignment_objectives", "assignments", "grades",
            "revision_reminders_sent",
        ])
        self.assertEqual(self._revision_rows(context["revision_report"]), [])
        self.assertEqual(context["students"][0]["objective_total"], 1)
        self.assertEqual(self._cells(context["students"][0]), [
            {"id": "lo-2", "vendor_code": "A1", "required_ms": 1, "m_count": 0, "is_passed": False},
        ])
        self.assertNotIn("homework_scores", calls)
        self.assertEqual(calls.count("assignments"), 1)

    def test_revision_grades_select_includes_homework_group_and_class_id(self):
        _response, _calls, _context, selects = self._page(self._full_store(), self.CLASS_ID)
        revision_selects = [
            text for table, text in selects
            if table == "grades" and "hw_score_at_entry" in text
        ]
        self.assertEqual(len(revision_selects), 1)
        text = revision_selects[0]
        embed_at = text.find("assignments(")
        embed = text[embed_at:text.find(")", embed_at) + 1]
        self.assertIn("homework_group", embed)
        self.assertIn("class_id", embed)
