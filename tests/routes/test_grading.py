import os
import io
import unittest
import unittest.mock
from unittest.mock import MagicMock

from app.models import Course, Homework
from app.helpers.csv_text import MAX_IMPORT_ROWS
from app.helpers.gradesheet_csv import (
    _enrolled_import_name_index,
    _lookup_enrolled_import_student_id,
)
from app.helpers.free_passes import _promote_non_counting_masteries_for_student
from app.helpers.grade_log import _log_grade_deletions
from app import create_app
from tests.routes.fakes import _class_detail_client


class TestSaveGradesAutoConvertHwGuard(unittest.TestCase):
    """save_grades: stamps counts_for_mastery at entry time on first-entry M/MR.

    The pre-existing tests asserted the *old* M→I rewrite. The new contract
    keeps the letter as M / MR and instead persists a counts_for_mastery flag
    that is sticky for the lifetime of the row. See
    scripts/add_grades_counts_for_mastery.sql.
    """

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def _patched_save_grades_call(self, hw_map, grade="M"):
        """Common scaffolding: returns the rows the route attempted to upsert."""
        from app.routes import grading as r
        captured_rows = []
        exec_m = MagicMock()
        q = MagicMock()
        q.upsert.side_effect = lambda rows, **kw: captured_rows.extend(rows) or exec_m
        # Existing-row pre-fetch in the new save_grades returns no rows by
        # default; MagicMock's __iter__ yields []. That's exactly the
        # "first-entry M" path we want to exercise here.
        sa = MagicMock()
        sa.table.return_value = q

        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["csrf_token"] = "test-csrf"

        headers = {"X-CSRF-Token": "test-csrf"}

        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_assignment_belongs_to_class", return_value=True), \
                unittest.mock.patch.object(r, "_class_auto_convert_m_enabled", return_value=True), \
                unittest.mock.patch.object(
                    r, "_enrolled_student_ids_for_class", return_value={"stu-1"}
                ), \
                unittest.mock.patch.object(Course, "get_assignment_lo_ids", return_value=["lo-x"]), \
                unittest.mock.patch.object(
                    Homework,
                    "get_hw_scores_map_for_assignment",
                    return_value=hw_map,
                ):
            rv = self.client.post(
                "/api/class/class-1/save-grades",
                json={"assignment_id": "asg-1", "grades": {"stu-1|lo-x": grade}},
                headers=headers,
            )
        return rv, captured_rows

    def test_missing_hw_rejects_m_and_allows_a(self):
        rv_m, rows_m = self._patched_save_grades_call(hw_map={}, grade="M")
        self.assertEqual(rv_m.status_code, 400)
        self.assertEqual(rows_m, [])
        body = rv_m.get_json()
        self.assertFalse(body.get("success"))
        self.assertIn("homework", (body.get("error") or "").lower())

        rv_a, rows_a = self._patched_save_grades_call(hw_map={}, grade="A")
        self.assertEqual(rv_a.status_code, 200)
        self.assertEqual(len(rows_a), 1)
        self.assertEqual(rows_a[0]["top_score"], "A")

    def test_hw_zero_is_recorded_and_allows_m(self):
        rv, captured_rows = self._patched_save_grades_call(hw_map={"stu-1": 0})
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(captured_rows[0]["top_score"], "M")
        self.assertEqual(captured_rows[0]["counts_for_mastery"], False)

    def test_hw_below_threshold_keeps_m_but_marks_non_counting(self):
        rv, captured_rows = self._patched_save_grades_call(hw_map={"stu-1": 40})
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(len(captured_rows), 1)
        # Letter stays M (the I letter is now a print-only visual on Reports).
        self.assertEqual(captured_rows[0]["top_score"], "M")
        # Non-counting flag captured at entry time; sticky going forward.
        self.assertEqual(captured_rows[0]["counts_for_mastery"], False)

    def test_hw_above_threshold_keeps_m_and_marks_counting(self):
        rv, captured_rows = self._patched_save_grades_call(hw_map={"stu-1": 80})
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(len(captured_rows), 1)
        self.assertEqual(captured_rows[0]["top_score"], "M")
        self.assertEqual(captured_rows[0]["counts_for_mastery"], True)

    def test_auto_convert_does_not_read_previous_hw(self):
        with unittest.mock.patch.object(Homework, "decide_hw_score_update") as decide:
            rv_low, rows_low = self._patched_save_grades_call(hw_map={"stu-1": 64})
            rv_high, rows_high = self._patched_save_grades_call(hw_map={"stu-1": 80})
        decide.assert_not_called()
        self.assertEqual(rv_low.status_code, 200)
        self.assertEqual(rows_low[0]["counts_for_mastery"], False)
        self.assertEqual(rv_high.status_code, 200)
        self.assertEqual(rows_high[0]["counts_for_mastery"], True)

    def test_save_grades_stores_q_when_homework_is_recorded(self):
        rv_missing, rows_missing = self._patched_save_grades_call(hw_map={}, grade="Q")
        self.assertEqual(rv_missing.status_code, 400)
        self.assertEqual(rows_missing, [])

        rv, rows = self._patched_save_grades_call(hw_map={"stu-1": 80}, grade="q")
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["top_score"], "Q")
        self.assertTrue(rows[0]["counts_for_mastery"])

        rv_z, rows_z = self._patched_save_grades_call(hw_map={"stu-1": 80}, grade="Z")
        self.assertEqual(rv_z.status_code, 200)
        self.assertEqual(rows_z, [])


class TestHwPassPromotesNonCountingMasteries(unittest.TestCase):
    """save_hw_percentage with score=-1 flips non-counting M/MR to counting."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_hw_pass_promotes_non_counting_masteries(self):
        from app.routes import grading as r

        captured_upserts = []

        def make_query(table_name):
            q = MagicMock()
            if table_name == "homework_scores":
                q.upsert.return_value.execute.return_value = MagicMock()
            elif table_name == "grades":
                q.select.return_value.eq.return_value.in_.return_value.in_.return_value.execute.return_value = MagicMock(
                    data=[
                        {
                            "student_id": "stu-1",
                            "learning_objective_id": "lo-x",
                            "assignment_id": "asg-1",
                            "top_score": "M",
                            "counts_for_mastery": False,
                        }
                    ]
                )

                def upsert(rows, **kw):
                    captured_upserts.extend(rows)
                    return MagicMock(execute=MagicMock(return_value=MagicMock()))

                q.upsert.side_effect = upsert
            return q

        sa = MagicMock()
        sa.table.side_effect = make_query

        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["csrf_token"] = "test-csrf"

        headers = {"X-CSRF-Token": "test-csrf"}

        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch("app.helpers.free_passes.supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(
                    r, "_student_enrolled_in_class", return_value=True
                ), \
                unittest.mock.patch.object(
                    Homework,
                    "resolve_hw_group_storage_key",
                    return_value="hw-g1",
                ), \
                unittest.mock.patch.object(
                    Course,
                    "get_all_lo_ids_for_class",
                    return_value=["lo-x"],
                ):
            rv = self.client.post(
                "/api/class/class-1/save-hw-percentage",
                json={
                    "student_id": "stu-1",
                    "score": -1,
                    "assignment_id": "asg-1",
                },
                headers=headers,
            )

        self.assertEqual(rv.status_code, 200)
        body = rv.get_json()
        self.assertTrue(body.get("success"))
        self.assertEqual(body.get("promoted_masteries"), 1)
        self.assertEqual(len(captured_upserts), 1)
        self.assertTrue(captured_upserts[0].get("counts_for_mastery"))

    def test_typed_score_does_not_write_previous_or_shift(self):
        from app.routes import grading as r

        captured = []

        def make_query(table_name):
            q = MagicMock()
            if table_name == "homework_scores":
                def upsert(rows, **kwargs):
                    captured.extend(rows)
                    return MagicMock(execute=MagicMock(return_value=MagicMock()))
                q.upsert.side_effect = upsert
            return q

        sa = MagicMock()
        sa.table.side_effect = make_query

        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["csrf_token"] = "test-csrf"

        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(
                    r, "_student_enrolled_in_class", return_value=True
                ), \
                unittest.mock.patch.object(
                    r, "_assignment_belongs_to_class", return_value=True
                ), \
                unittest.mock.patch.object(
                    Homework,
                    "resolve_hw_group_storage_key",
                    return_value="hw-g1",
                ), \
                unittest.mock.patch.object(Homework, "decide_hw_score_update") as decide:
            rv = self.client.post(
                "/api/class/class-1/save-hw-percentage",
                json={
                    "student_id": "stu-1",
                    "score": 91,
                    "assignment_id": "asg-1",
                    "prev_score_pct": 1,
                },
                headers={"X-CSRF-Token": "test-csrf"},
            )

        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.get_json().get("success"))
        decide.assert_not_called()
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["score_pct"], 91)
        self.assertNotIn("prev_score_pct", captured[0])


class _GradesApiQuery:
    def __init__(self, rows, calls, table):
        self._rows = [dict(row) for row in rows]
        self._calls = calls
        self._table = table
        self._order = None
        self._window = None
        self._filters = []

    def select(self, *args, **kwargs):
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        self._rows = [row for row in self._rows if row.get(col) == val]
        return self

    def is_(self, col, val):
        self._filters.append(("is", col, val))
        if val is None:
            self._rows = [row for row in self._rows if row.get(col) is None]
        else:
            self._rows = [row for row in self._rows if row.get(col) is val]
        return self

    def in_(self, col, values):
        self._filters.append(("in", col, list(values)))
        allowed = set(values)
        self._rows = [row for row in self._rows if row.get(col) in allowed]
        return self

    def limit(self, count):
        self._rows = self._rows[:count]
        return self

    def order(self, column, desc=False):
        self._order = (column, desc)
        return self

    def range(self, start, end):
        self._window = (start, end)
        return self

    def execute(self):
        self._calls.append({"table": self._table, "filters": list(self._filters)})
        rows = list(self._rows)
        if self._order:
            column, desc = self._order
            rows.sort(
                key=lambda row: (row.get(column) is None, str(row.get(column))),
                reverse=desc,
            )
        if self._window:
            start, end = self._window
            rows = rows[start:end + 1]
        result = unittest.mock.MagicMock()
        result.data = rows
        return result


def _grades_api_client(store, calls):
    admin = unittest.mock.MagicMock()

    def table(name):
        return _GradesApiQuery(store.get(name, []), calls, name)

    admin.table.side_effect = table
    return admin


class TestAssignmentGradesResponse(unittest.TestCase):
    CLASS_ID = "class-grades"
    LINKED = "asg-linked"
    SIB_A = "asg-sib-a"
    SIB_B = "asg-sib-b"
    NO_LINK = "asg-nolink"
    NO_GROUP = "asg-nogroup"
    LO_LINK = "lo-link"
    LO_CLASS = "lo-class"
    LO_OTHER = "lo-other"
    STU_IN = "stu-in"
    STU_OUT = "stu-out"
    STU_LEGACY = "stu-legacy"
    STU_BLANK = "stu-blank"
    STU_SIB = "stu-sib"
    STU_PASS = "stu-pass"
    STU_LOW = "stu-low"
    STU_MARK = "stu-mark"

    def _store(self):
        return {
            "assignments": [
                {"id": self.LINKED, "class_id": self.CLASS_ID, "homework_group": "HW 1", "assignment_type": "exam"},
                {"id": self.SIB_A, "class_id": self.CLASS_ID, "homework_group": "HW 1", "assignment_type": "mastery_opp"},
                {"id": self.SIB_B, "class_id": self.CLASS_ID, "homework_group": "HW 1", "assignment_type": "mastery_opp"},
                {"id": self.NO_LINK, "class_id": self.CLASS_ID, "homework_group": "", "assignment_type": "mastery_opp"},
                {"id": self.NO_GROUP, "class_id": self.CLASS_ID, "homework_group": None, "assignment_type": "project"},
            ],
            "assignment_objectives": [
                {"assignment_id": self.LINKED, "learning_objective_id": self.LO_LINK},
                {"assignment_id": self.NO_GROUP, "learning_objective_id": self.LO_LINK},
            ],
            "learning_objectives": [
                {"id": self.LO_LINK, "class_id": self.CLASS_ID},
                {"id": self.LO_CLASS, "class_id": self.CLASS_ID},
                {"id": self.LO_OTHER, "class_id": "class-other"},
            ],
            "grades": [
                {"student_id": self.STU_IN, "learning_objective_id": self.LO_LINK, "top_score": "M", "counts_for_mastery": True, "assignment_id": self.LINKED},
                {"student_id": self.STU_IN, "learning_objective_id": self.LO_LINK, "top_score": "R", "counts_for_mastery": False, "assignment_id": None},
                {"student_id": self.STU_OUT, "learning_objective_id": self.LO_LINK, "top_score": "P", "counts_for_mastery": None, "assignment_id": self.LINKED},
                {"student_id": self.STU_LEGACY, "learning_objective_id": self.LO_LINK, "top_score": "X", "counts_for_mastery": False, "assignment_id": None},
                {"student_id": self.STU_BLANK, "learning_objective_id": self.LO_LINK, "top_score": "", "counts_for_mastery": False, "assignment_id": self.LINKED},
                {"student_id": self.STU_IN, "learning_objective_id": self.LO_CLASS, "top_score": "R", "counts_for_mastery": True, "assignment_id": None},
                {"student_id": self.STU_OUT, "learning_objective_id": self.LO_OTHER, "top_score": "M", "counts_for_mastery": True, "assignment_id": None},
                {"student_id": self.STU_IN, "learning_objective_id": self.LO_LINK, "top_score": "A", "counts_for_mastery": True, "assignment_id": self.NO_GROUP},
            ],
            "homework_scores": [
                {"id": "hs-1", "class_id": self.CLASS_ID, "student_id": self.STU_IN, "homework_group": self.SIB_A, "score_pct": 10, "prev_score_pct": 1},
                {"id": "hs-9", "class_id": self.CLASS_ID, "student_id": self.STU_IN, "homework_group": self.SIB_B, "score_pct": 99, "prev_score_pct": 98},
                {"id": "hs-2", "class_id": self.CLASS_ID, "student_id": self.STU_IN, "homework_group": "HW 1", "score_pct": 40, "prev_score_pct": 4},
                {"id": "hs-1b", "class_id": self.CLASS_ID, "student_id": self.STU_SIB, "homework_group": self.SIB_A, "score_pct": 11, "prev_score_pct": 8},
                {"id": "hs-9b", "class_id": self.CLASS_ID, "student_id": self.STU_SIB, "homework_group": self.SIB_B, "score_pct": 22, "prev_score_pct": 9},
                {"id": "hs-pass", "class_id": self.CLASS_ID, "student_id": self.STU_PASS, "homework_group": "HW 1", "score_pct": -1, "prev_score_pct": 5},
                {"id": "hs-low", "class_id": self.CLASS_ID, "student_id": self.STU_LOW, "homework_group": "HW 1", "score_pct": 64, "prev_score_pct": 64},
                {"id": "hs-mark", "class_id": self.CLASS_ID, "student_id": self.STU_MARK, "homework_group": "HW 1", "score_pct": 65, "prev_score_pct": None},
                {"id": "hs-nolink", "class_id": self.CLASS_ID, "student_id": self.STU_IN, "homework_group": self.NO_LINK, "score_pct": 70, "prev_score_pct": 3},
                {"id": "hs-nogroup", "class_id": self.CLASS_ID, "student_id": self.STU_IN, "homework_group": self.NO_GROUP, "score_pct": 80, "prev_score_pct": 7},
            ],
        }

    def _get(self, assignment_id):
        from app import models
        from app.routes import grading as r
        calls = []
        admin = _grades_api_client(self._store(), calls)
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst-1"
            sess["role"] = "instructor"
        with unittest.mock.patch.object(r, "supabase_admin", admin), \
                unittest.mock.patch.object(models, "supabase_admin", admin), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True):
            response = client.get(
                "/api/class/%s/assignment/%s/grades" % (self.CLASS_ID, assignment_id)
            )
        return response, calls

    def test_group_legacy_null_and_outsider(self):
        response, _calls = self._get(self.LINKED)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "success": True,
            "grades": {
                "%s|%s" % (self.STU_IN, self.LO_LINK): "M",
                "%s|%s" % (self.STU_OUT, self.LO_LINK): "P",
                "%s|%s" % (self.STU_LEGACY, self.LO_LINK): "X",
                "%s|%s" % (self.STU_BLANK, self.LO_LINK): "",
            },
            "has_assignment_grades": True,
            "counts_for_mastery_map": {
                "%s|%s" % (self.STU_IN, self.LO_LINK): True,
                "%s|%s" % (self.STU_OUT, self.LO_LINK): True,
                "%s|%s" % (self.STU_LEGACY, self.LO_LINK): False,
                "%s|%s" % (self.STU_BLANK, self.LO_LINK): False,
            },
            "hw_scores": {
                self.STU_IN: 40,
                self.STU_SIB: 22,
                self.STU_PASS: -1,
                self.STU_LOW: 64,
                self.STU_MARK: 65,
            },
            "hw_prev_scores": {
                self.STU_IN: 4,
                self.STU_SIB: 9,
                self.STU_PASS: 5,
                self.STU_LOW: 64,
                self.STU_MARK: None,
            },
            "revision_eligible": {
                self.STU_IN: False,
                self.STU_SIB: False,
                self.STU_PASS: True,
                self.STU_LOW: False,
                self.STU_MARK: True,
            },
            "assignment_type": "exam",
        })

    def test_no_objective_links_use_class_objectives(self):
        response, _calls = self._get(self.NO_LINK)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "success": True,
            "grades": {
                "%s|%s" % (self.STU_IN, self.LO_LINK): "R",
                "%s|%s" % (self.STU_IN, self.LO_CLASS): "R",
                "%s|%s" % (self.STU_LEGACY, self.LO_LINK): "X",
            },
            "has_assignment_grades": False,
            "counts_for_mastery_map": {
                "%s|%s" % (self.STU_IN, self.LO_LINK): False,
                "%s|%s" % (self.STU_IN, self.LO_CLASS): True,
                "%s|%s" % (self.STU_LEGACY, self.LO_LINK): False,
            },
            "hw_scores": {self.STU_IN: 70},
            "hw_prev_scores": {self.STU_IN: 3},
            "revision_eligible": {self.STU_IN: True},
            "assignment_type": "mastery_opp",
        })

    def test_no_homework_group_uses_the_assignment_id(self):
        response, _calls = self._get(self.NO_GROUP)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "success": True,
            "grades": {
                "%s|%s" % (self.STU_IN, self.LO_LINK): "A",
                "%s|%s" % (self.STU_LEGACY, self.LO_LINK): "X",
            },
            "has_assignment_grades": True,
            "counts_for_mastery_map": {
                "%s|%s" % (self.STU_IN, self.LO_LINK): True,
                "%s|%s" % (self.STU_LEGACY, self.LO_LINK): False,
            },
            "hw_scores": {self.STU_IN: 80},
            "hw_prev_scores": {self.STU_IN: 7},
            "revision_eligible": {self.STU_IN: True},
            "assignment_type": "project",
        })

    def test_group_fixture_reads_each_table_once_for_its_job(self):
        _response, calls = self._get(self.LINKED)
        self.assertEqual(
            [call["table"] for call in calls],
            [
                "assignments",
                "grades",
                "assignment_objectives",
                "grades",
                "assignments",
                "homework_scores",
            ],
        )

    def test_no_group_fixture_skips_the_sibling_read(self):
        _response, calls = self._get(self.NO_GROUP)
        self.assertEqual(
            [call["table"] for call in calls],
            [
                "assignments",
                "grades",
                "assignment_objectives",
                "grades",
                "homework_scores",
            ],
        )


class TestImportRosterNameMatch(unittest.TestCase):
    def test_last_first_export_name_hits_stored_first_last(self):
        index = _enrolled_import_name_index([
            {"id": "stu-1", "full_name": "Doe, Jane"},
        ])
        self.assertEqual(_lookup_enrolled_import_student_id("Doe, Jane", index), "stu-1")
        self.assertEqual(_lookup_enrolled_import_student_id("Doe,Jane", index), "stu-1")
        self.assertIsNone(_lookup_enrolled_import_student_id("Jane Doe", index))
        self.assertIsNone(_lookup_enrolled_import_student_id("Nobody", index))

    def test_enrolled_doe_jane_matches_doe_comma_jane_without_a_space(self):
        index = _enrolled_import_name_index([
            {"id": "stu-1", "full_name": "Doe, Jane"},
        ])
        self.assertEqual(
            _lookup_enrolled_import_student_id("Doe,Jane", index),
            "stu-1",
        )

    def test_enrolled_doe_jane_does_not_match_jane_doe(self):
        index = _enrolled_import_name_index([
            {"id": "stu-1", "full_name": "Doe, Jane"},
        ])
        self.assertIsNone(_lookup_enrolled_import_student_id("Jane Doe", index))

    def _post_grade_import(
        self, students, enroll_rows, extra=None, lo_rows=None, learning_objectives=None,
        hw_map=None,
    ):
        from app.routes import grading as r
        if hw_map is None:
            hw_map = {}
        self.hw_upserts = []
        if lo_rows is None:
            lo_rows = [{
                "id": "lo-1",
                "vendor_code": "D1",
                "description": None,
            }]
        if learning_objectives is None:
            learning_objectives = ["D1"]

        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        writes = []
        grade_rows = []
        grades = MagicMock()

        def upsert_grades(rows, **kwargs):
            grade_rows.extend(rows)
            return grades

        grades.upsert.side_effect = upsert_grades
        grades.execute.return_value = MagicMock(data=[])

        class _EnrollQuery:
            def __init__(self):
                self._class_id = None

            def select(self, *args, **kwargs):
                return self

            def eq(self, field, val):
                if field == "class_id":
                    self._class_id = val
                return self

            def insert(self, *args, **kwargs):
                writes.append("enrollments.insert")
                return self

            def upsert(self, *args, **kwargs):
                writes.append("enrollments.upsert")
                return self

            def execute(self):
                data = [
                    row for row in enroll_rows
                    if row.get("class_id") == self._class_id
                ]
                return MagicMock(data=data)

        def table(name):
            if name == "grades":
                return grades
            if name == "enrollments":
                return _EnrollQuery()
            if name == "homework_scores":
                query = MagicMock()

                def upsert_hw(rows, **kwargs):
                    self.hw_upserts.extend(rows if isinstance(rows, list) else [rows])
                    writes.append("homework_scores.upsert")
                    return query

                query.upsert.side_effect = upsert_hw
                query.delete.side_effect = lambda *a, **k: writes.append("homework_scores.delete") or query
                query.select.return_value = query
                query.eq.return_value = query
                query.in_.return_value = query
                query.execute.return_value = MagicMock(data=[])
                return query
            query = MagicMock()
            query.insert.side_effect = lambda *a, **k: writes.append(name + ".insert") or query
            query.upsert.side_effect = lambda *a, **k: writes.append(name + ".upsert") or query
            if name == "learning_objectives":
                query.select.return_value = query
                query.eq.return_value = query
                query.execute.return_value = MagicMock(data=lo_rows)
            elif name == "assignment_objectives":
                query.execute.return_value = MagicMock(data=[{
                    "learning_objective_id": "lo-1",
                }])
            else:
                query.execute.return_value = MagicMock(data=[])
            return query

        sa = MagicMock()
        sa.table.side_effect = table
        with client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["csrf_token"] = "test-csrf"
        payload = {
            "class_id": "class-a",
            "assignment_id": "asg-1",
            "students": students,
            "learning_objectives": learning_objectives,
        }
        if extra:
            payload.update(extra)
        with unittest.mock.patch("app.helpers.rate_limit._rate_limit", return_value=True), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_assignment_belongs_to_class", return_value=True), \
                unittest.mock.patch.object(
                    Homework, "resolve_hw_group_storage_key", return_value="quiz-1"
                ), \
                unittest.mock.patch.object(
                    Homework, "get_hw_scores_map_for_assignment", return_value=hw_map
                ), \
                unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch("app.helpers.lo_import.supabase_admin", sa):
            rv = client.post(
                "/api/import-grades",
                json=payload,
                headers={"X-CSRF-Token": "test-csrf"},
            )
        return rv, grades, writes, grade_rows

    def test_whitespace_only_objective_is_not_inserted(self):
        rv, _grades, writes, _grade_rows = self._post_grade_import(
            [],
            [],
            learning_objectives=["   ", ""],
        )
        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.get_json()["success"])
        self.assertNotIn("learning_objectives.insert", writes)

    def test_same_name_enrolled_only_in_another_class_is_not_a_match(self):
        rv, grades, writes, grade_rows = self._post_grade_import(
            [{"name": "Doe, Jane", "grades": {"D1": "M"}}],
            [
                {
                    "student_id": "stu-b",
                    "class_id": "class-b",
                    "profiles": {"id": "stu-b", "full_name": "Doe, Jane"},
                },
                {
                    "student_id": "stu-a",
                    "class_id": "class-a",
                    "profiles": {"id": "stu-a", "full_name": "Smith, Alex"},
                },
            ],
        )
        self.assertEqual(rv.status_code, 409)
        self.assertEqual(rv.get_json(), {
            "success": False,
            "error": "Some names need confirmation",
            "unresolved_names": ["Doe, Jane"],
        })
        grades.insert.assert_not_called()
        grades.upsert.assert_not_called()
        self.assertEqual(grade_rows, [])
        self.assertEqual(writes, [])

    def test_two_enrolled_students_with_the_same_name_key_return_409(self):
        rv, grades, writes, grade_rows = self._post_grade_import(
            [{"name": "Doe, Jane", "grades": {"D1": "M"}}],
            [
                {
                    "student_id": "stu-1",
                    "class_id": "class-a",
                    "profiles": {"id": "stu-1", "full_name": "Doe, Jane"},
                },
                {
                    "student_id": "stu-2",
                    "class_id": "class-a",
                    "profiles": {"id": "stu-2", "full_name": "Doe,Jane"},
                },
            ],
        )
        self.assertEqual(rv.status_code, 409)
        self.assertEqual(
            rv.get_json(),
            {"error": "ambiguous_name", "name": "Doe, Jane"},
        )
        grades.insert.assert_not_called()
        grades.upsert.assert_not_called()
        self.assertEqual(grade_rows, [])
        self.assertEqual(writes, [])

    def test_unmatched_name_without_ignored_names_is_unresolved(self):
        rv, grades, writes, grade_rows = self._post_grade_import(
            [{"name": "Doe, Jane", "grades": {"D1": "A"}}],
            [],
        )
        self.assertEqual(rv.status_code, 409)
        self.assertEqual(rv.get_json(), {
            "success": False,
            "error": "Some names need confirmation",
            "unresolved_names": ["Doe, Jane"],
        })
        grades.insert.assert_not_called()
        grades.upsert.assert_not_called()
        self.assertEqual(grade_rows, [])
        self.assertEqual(writes, [])

    def test_ignored_name_is_dropped_and_matched_name_is_imported(self):
        rv, grades, writes, grade_rows = self._post_grade_import(
            [
                {"name": "Doe, Jane", "grades": {"D1": "A"}},
                {"name": "Roe, Richard", "grades": {"D1": "A"}},
            ],
            [{
                "student_id": "stu-1",
                "class_id": "class-a",
                "profiles": {"id": "stu-1", "full_name": "Doe, Jane"},
            }],
            {"ignored_names": ["Roe,Richard"]},
        )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual([row["student_id"] for row in grade_rows], ["stu-1"])
        self.assertEqual([row["top_score"] for row in grade_rows], ["A"])
        self.assertNotIn("profiles.insert", writes)
        self.assertNotIn("enrollments.insert", writes)
        self.assertNotIn("enrollment_attach_log.insert", writes)

    def test_matched_name_in_ignored_names_is_still_imported(self):
        rv, grades, writes, grade_rows = self._post_grade_import(
            [{"name": "Doe,Jane", "grades": {"D1": "A"}}],
            [{
                "student_id": "stu-1",
                "class_id": "class-a",
                "profiles": {"id": "stu-1", "full_name": "Doe, Jane"},
            }],
            {"ignored_names": ["Doe, Jane"]},
        )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual([row["student_id"] for row in grade_rows], ["stu-1"])
        self.assertNotIn("profiles.insert", writes)
        self.assertNotIn("enrollments.insert", writes)

    def test_name_resolutions_field_does_not_resolve_an_unmatched_name(self):
        rv, grades, writes, grade_rows = self._post_grade_import(
            [{"name": "Doe, Jane", "grades": {"D1": "A"}}],
            [],
            {"name_resolutions": {"doe, jane": {"action": "create"}}},
        )
        self.assertEqual(rv.status_code, 409)
        self.assertEqual(rv.get_json(), {
            "success": False,
            "error": "Some names need confirmation",
            "unresolved_names": ["Doe, Jane"],
        })
        grades.insert.assert_not_called()
        grades.upsert.assert_not_called()
        self.assertEqual(grade_rows, [])
        self.assertEqual(writes, [])

    def test_malformed_ignored_names_are_400(self):
        enrolled = [{
            "student_id": "stu-1",
            "class_id": "class-a",
            "profiles": {"id": "stu-1", "full_name": "Doe, Jane"},
        }]
        students = [{"name": "Doe, Jane", "grades": {"D1": "A"}}]
        cases = [
            "Doe, Jane",
            ["x"] * (MAX_IMPORT_ROWS + 1),
            [123],
            ["A" * 256],
        ]
        for ignored in cases:
            rv, grades, writes, grade_rows = self._post_grade_import(
                students,
                enrolled,
                {"ignored_names": ignored},
            )
            self.assertEqual(rv.status_code, 400)
            self.assertFalse(rv.get_json()["success"])
            grades.upsert.assert_not_called()
            self.assertEqual(grade_rows, [])
            self.assertNotIn("profiles.insert", writes)
            self.assertNotIn("enrollments.insert", writes)

    def test_same_grades_under_same_codes_ignore_column_order(self):
        enrolled = [{
            "student_id": "stu-1",
            "class_id": "class-a",
            "profiles": {"id": "stu-1", "full_name": "Doe, Jane"},
        }]
        los = [
            {"id": "lo-ao10", "vendor_code": "AO10", "description": None},
            {"id": "lo-ao2", "vendor_code": "AO2", "description": None},
        ]

        def assigned(rows):
            return sorted(
                (row["learning_objective_id"], row["top_score"]) for row in rows
            )

        rv_old, _, _, rows_old = self._post_grade_import(
            [{"name": "Doe, Jane", "grades": {"AO10": "R", "AO2": "M"}, "homework_pct": "80"}],
            enrolled,
            lo_rows=los,
            learning_objectives=["AO10", "AO2"],
        )
        rv_new, _, _, rows_new = self._post_grade_import(
            [{"name": "Doe, Jane", "grades": {"AO2": "M", "AO10": "R"}, "homework_pct": "80"}],
            enrolled,
            lo_rows=los,
            learning_objectives=["AO2", "AO10"],
        )
        self.assertEqual(rv_old.status_code, 200)
        self.assertEqual(rv_new.status_code, 200)
        self.assertEqual(assigned(rows_old), [("lo-ao10", "R"), ("lo-ao2", "M")])
        self.assertEqual(assigned(rows_old), assigned(rows_new))

    def _enrolled_jane(self):
        return [{
            "student_id": "stu-1",
            "class_id": "class-a",
            "profiles": {"id": "stu-1", "full_name": "Doe, Jane"},
        }]

    def test_import_does_not_insert_grade_change_log(self):
        rv, _, writes, grade_rows = self._post_grade_import(
            [{"name": "Doe, Jane", "grades": {"D1": "M"}}],
            self._enrolled_jane(),
            hw_map={"stu-1": 80},
        )
        self.assertEqual(rv.status_code, 200)
        self.assertTrue(grade_rows)
        self.assertEqual(grade_rows[0]["last_modified_by"], "inst1")
        self.assertNotIn("grade_change_log.insert", writes)

    def test_first_import_sets_current_and_previous_equal(self):
        rv, _, writes, grade_rows = self._post_grade_import(
            [{
                "name": "Doe, Jane",
                "grades": {"D1": "M"},
                "homework_pct": "80",
                "prev_score_pct": 1,
            }],
            self._enrolled_jane(),
        )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(len(self.hw_upserts), 1)
        self.assertEqual(self.hw_upserts[0]["score_pct"], 80)
        self.assertEqual(self.hw_upserts[0]["prev_score_pct"], 80)
        self.assertEqual(grade_rows[0]["hw_score_at_entry"], 80)
        self.assertNotIn("homework_scores.delete", writes)

    def test_changed_import_moves_stored_current_into_previous(self):
        rv, _, _, _ = self._post_grade_import(
            [{"name": "Doe, Jane", "grades": {"D1": "M"}, "homework_pct": "90"}],
            self._enrolled_jane(),
            hw_map={"stu-1": 70},
        )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(self.hw_upserts[0]["score_pct"], 90)
        self.assertEqual(self.hw_upserts[0]["prev_score_pct"], 70)

    def test_identical_import_does_not_write(self):
        rv, _, writes, _ = self._post_grade_import(
            [{"name": "Doe, Jane", "grades": {"D1": "M"}, "homework_pct": "70"}],
            self._enrolled_jane(),
            hw_map={"stu-1": 70},
        )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(self.hw_upserts, [])
        self.assertNotIn("homework_scores.upsert", writes)

    def test_pass_to_zero_keeps_stored_previous(self):
        rv, _, _, _ = self._post_grade_import(
            [{"name": "Doe, Jane", "grades": {"D1": "A"}, "homework_pct": "0"}],
            self._enrolled_jane(),
            hw_map={"stu-1": -1},
        )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(self.hw_upserts[0]["score_pct"], 0)
        self.assertEqual(self.hw_upserts[0]["prev_score_pct"], -1)

    def test_blank_hw_cell_does_not_delete_or_shift(self):
        rv, _, writes, grade_rows = self._post_grade_import(
            [{"name": "Doe, Jane", "grades": {"D1": "M"}, "homework_pct": None}],
            self._enrolled_jane(),
            hw_map={"stu-1": 80},
        )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(self.hw_upserts, [])
        self.assertNotIn("homework_scores.delete", writes)
        self.assertEqual(grade_rows[0]["hw_score_at_entry"], 80)


class TestUpdateGradeRoster(unittest.TestCase):
    def test_update_grade_roster_is_id_and_full_name_including_muted(self):
        from app.routes import grading as r

        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        class_data = {
            "name": "Algebra",
            "enrollments": [
                {
                    "muted": False,
                    "profiles": {
                        "id": "stu-active",
                        "full_name": "Zenith, Bob",
                        "email": "bob@example.com",
                        "role": "student",
                    },
                },
                {
                    "muted": True,
                    "profiles": {
                        "id": "stu-muted",
                        "full_name": "Adams, Zoe",
                        "email": "zoe@example.com",
                        "role": "student",
                    },
                },
                {
                    "muted": False,
                    "profiles": {
                        "id": "  ",
                        "full_name": "Blank, Id",
                        "email": "blank@example.com",
                    },
                },
            ],
        }
        captured = {}

        def fake_render(template_name, **kwargs):
            captured["template"] = template_name
            captured["roster"] = kwargs.get("roster")
            return "ok"

        with client.session_transaction() as sess:
            sess["user_id"] = "inst1"
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(
                    Course, "load_class_record", return_value={"name": "Algebra"},
                ), \
                unittest.mock.patch.object(
                    Course, "load_enrollments", return_value=class_data["enrollments"],
                ), \
                unittest.mock.patch.object(r, "load_assignments_for_class", return_value=[]), \
                unittest.mock.patch.object(r, "render_template", side_effect=fake_render):
            rv = client.get("/class/class-a/update_grade")
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(captured["template"], "classes/update_grade.html")
        roster = captured["roster"]
        self.assertEqual(roster, [
            {"id": "stu-muted", "full_name": "Adams, Zoe"},
            {"id": "stu-active", "full_name": "Zenith, Bob"},
        ])
        for row in roster:
            self.assertEqual(set(row.keys()), {"id", "full_name"})

    def test_update_grade_post_is_not_allowed(self):
        from app.routes import grading as r

        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True):
            rv = client.post(
                "/class/class-a/update_grade",
                data={"confirmed": "true"},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 405)

    def test_update_grade_template_roster_script_and_analyzer_version(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "templates", "classes", "update_grade.html",
        )
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn(
            '<script type="application/json" id="classRosterJson">{{ roster|tojson }}</script>',
            src,
        )
        self.assertIn("csv_wizard.js') }}?v=23", src)

    def test_speed_grader_autosave_runs_when_the_tab_hides(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "static", "js", "class_speed_grader.js",
        )
        with open(path, encoding="utf-8") as fh:
            src = fh.read()

        def extract(name):
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

        save = extract("runAutosave")
        self.assertNotIn("document.hidden", src)
        self.assertLess(save.find("if (!hasUnsavedChanges) return;"), save.find("if (autosaveInFlight) return;"))
        self.assertLess(save.find("if (autosaveInFlight) return;"), save.find("autosaveInFlight = true;"))
        self.assertLess(save.find("autosaveInFlight = true;"), save.find("await performSave(null);"))
        self.assertLess(save.find("await performSave(null);"), save.find("autosaveInFlight = false;"))
        self.assertIn("window.setInterval(runAutosave, 180000);", src)
        self.assertIn("every 3 minutes", src)
        self.assertNotIn("every minute", src)
        self.assertNotIn("handleSessionExpired", src)

        handler_at = src.find("addEventListener('visibilitychange'")
        self.assertGreaterEqual(handler_at, 0)
        handler = src[handler_at:src.find("});", handler_at) + 3]
        self.assertIn("document.visibilityState === 'hidden'", handler)
        self.assertEqual(handler.count("runAutosave()"), 1)
        self.assertLess(handler.find("document.visibilityState === 'hidden'"), handler.find("runAutosave()"))
        self.assertNotIn("visible", handler)
        self.assertNotIn("performSave", handler)

    def test_shift_q_is_stored_q_and_plain_q_stays_rq(self):
        js_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "static", "js", "class_speed_grader.js",
        )
        html_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "templates", "classes", "class_speed_grader.html",
        )
        with open(js_path, encoding="utf-8") as handle:
            src = handle.read()
        with open(html_path, encoding="utf-8") as handle:
            html = handle.read()

        self.assertIn("const GRADE_MAP = { 'Q': 'RQ' };", src)
        self.assertNotIn("const GRADE_MAP = { 'Q': 'Q' }", src)

        wants_lines = [
            line for line in src.splitlines()
            if line.lstrip().startswith("const wantsGrade")
        ]
        self.assertEqual(len(wants_lines), 1)
        wants_line = wants_lines[0]
        shift_m = wants_line.find("(e.shiftKey && k === 'M') ? 'MR'")
        self.assertGreater(shift_m, -1)
        shift_q = wants_line.find("(e.shiftKey && k === 'Q') ? 'Q'")
        self.assertGreater(shift_q, -1)
        self.assertGreater(shift_q, shift_m)
        grade_map_lookup = wants_line.find("GRADE_MAP[k]")
        self.assertGreater(grade_map_lookup, -1)
        self.assertGreater(grade_map_lookup, shift_q)

        legend = html.find(">⇧Q</kbd>")
        self.assertGreater(legend, -1)
        end = html.find("</div>", legend)
        self.assertGreater(end, legend)
        legend_row = html[legend:end]
        self.assertIn("Revision done, retake pending", legend_row)
        self.assertNotIn("RQ", legend_row)
        self.assertIn("Revise with Question (RQ)", html)

        css_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "static", "css", "app.css",
        )
        with open(css_path, encoding="utf-8") as handle:
            css = handle.read()
        q_rule = ".mastery-q  { background: #fde047; color: #713f12; border-color: #a16207; }"
        q_at = css.find(q_rule)
        self.assertGreater(q_at, -1)
        self.assertNotIn(".mastery-q  { background: #ffedd5;", css)
        self.assertNotIn(q_rule, html)

    def test_revision_modal_q_chip_color(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "templates", "classes", "class_detail.html",
        )
        with open(path, encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn("revision-grade-{{ obj.top_score|lower }}", html)
        css_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "static", "css", "class_detail_print.css",
        )
        css = ""
        if os.path.isfile(css_path):
            with open(css_path, encoding="utf-8") as handle:
                css = handle.read()
        light = css.find(".revision-grade-q {")
        self.assertGreater(light, -1)
        light_end = css.find("}", light)
        self.assertGreater(light_end, light)
        light_rule = css[light:light_end]
        self.assertIn("background: #fde047", light_rule)
        self.assertIn("color: #713f12", light_rule)
        self.assertIn("border-color: #a16207", light_rule)
        self.assertNotIn("#ffedd5", light_rule)
        self.assertNotIn(".revision-grade-q {", html)
        dark = css.find(".dark .revision-grade-q {")
        self.assertGreater(dark, -1)
        self.assertGreater(dark, light)
        dark_end = css.find("}", dark)
        self.assertGreater(dark_end, dark)
        dark_rule = css[dark:dark_end]
        self.assertIn("background: #854d0e", dark_rule)
        self.assertIn("color: #fef9c3", dark_rule)
        self.assertIn("border-color: #facc15", dark_rule)

    def test_wizard_shift_q_enters_q_and_plain_q_stays_rq(self):
        js_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "static", "js", "csv_wizard.js",
        )
        html_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "templates", "classes", "update_grade.html",
        )
        with open(js_path, encoding="utf-8") as handle:
            src = handle.read()
        with open(html_path, encoding="utf-8") as handle:
            html = handle.read()

        valid = "const VALID_GRADES = new Set(['M', 'MR', 'R', 'Q', 'RQ', 'P', 'X', 'A']);"
        valid_at = src.find(valid)
        self.assertGreater(valid_at, -1)
        self.assertIn("'RQ'", valid)
        self.assertNotIn("'Z'", valid)
        self.assertIn("const GRADE_MAP  = { 'Q': 'RQ' };", src)
        self.assertNotIn("const GRADE_MAP  = { 'Q': 'Q' }", src)

        shift_m = src.find("if (e.shiftKey && k === 'M')")
        self.assertGreater(shift_m, -1)
        shift_q = src.find("} else if (e.shiftKey && k === 'Q')")
        self.assertGreater(shift_q, -1)
        self.assertGreater(shift_q, shift_m)
        grade_map_lookup = src.find("const grade = GRADE_MAP[k] || k;")
        self.assertGreater(grade_map_lookup, -1)
        self.assertGreater(grade_map_lookup, shift_q)

        self.assertIn("js/csv_wizard.js", html)
        self.assertNotIn("GRADE_MAP", html)
        legend = html.find(">⇧Q</kbd>")
        self.assertGreater(legend, -1)
        end = html.find("</span>", legend)
        self.assertGreater(end, legend)
        legend_row = html[legend:end]
        self.assertIn("Q, Revision done, retake pending", legend_row)
        self.assertNotIn("RQ", legend_row)
        self.assertIn(">Q</kbd> Revise with Question (RQ)", html)

        css_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "static", "css", "app.css",
        )
        with open(css_path, encoding="utf-8") as handle:
            css = handle.read()
        cell = css.find('#extractedStudentsTable .grade-cell[data-grade="Q"]  {')
        self.assertGreater(cell, -1)
        cell_end = css.find("}", cell)
        self.assertGreater(cell_end, cell)
        cell_rule = css[cell:cell_end]
        self.assertIn("background: #fde047", cell_rule)
        self.assertIn("color: #713f12", cell_rule)
        self.assertNotIn("#ffedd5", cell_rule)
        dark = css.find('.dark #extractedStudentsTable .grade-cell[data-grade="Q"]  {')
        self.assertGreater(dark, -1)
        self.assertGreater(dark, cell)
        dark_end = css.find("}", dark)
        self.assertGreater(dark_end, dark)
        dark_rule = css[dark:dark_end]
        self.assertIn("background: #854d0e", dark_rule)
        self.assertIn("color: #fef9c3", dark_rule)
        self.assertIn(
            '#extractedStudentsTable .grade-cell[data-grade="RQ"] { background: #ffedd5; color: #9a3412; }',
            css,
        )
        self.assertNotIn('.grade-cell[data-grade="Q"]', html)

    def test_csv_wizard_posts_ignored_names_without_preview(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "static", "js", "csv_wizard.js",
        )
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("preview-import-name-matches", src)
        self.assertNotIn("name_resolutions", src)
        self.assertIn("ignored_names", src)

    def test_csv_match_wizard_keeps_single_match_on_step_3(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "static", "js", "csv_wizard.js",
        )
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        analyze = src[src.find("async function analyzeCsv"):src.find("function displayExtractedLOs")]
        self.assertIn("goToStep(3)", analyze)
        self.assertIn("clearCsvAssignmentMatch()", analyze)
        self.assertIn("clearCsvAssignmentMatch()", src[src.find("function resetUploadChooser"):src.find("function handleFileSelect")])
        self.assertIn("csvMatchedAssignmentLine", src)
        self.assertIn("textContent", src[src.find("function syncCsvMatchedAssignmentLine"):src.find("function resetUploadChooser")])
        note = src[src.find("function syncCsvAssignmentMatchNote"):src.find("function syncCsvMatchedAssignmentLine")]
        self.assertIn("Matched from your file:", note)
        self.assertIn("matches more than one assignment", note)
        self.assertIn("textContent", note)
        err = src[src.find("function showUploadParseError"):src.find("function clearUploadParseError")]
        self.assertIn("textContent", err)
        self.assertNotIn("innerHTML", err)

    def test_csv_wizard_does_not_create_assignments(self):
        names = (
            os.path.join("templates", "classes", "update_grade.html"),
            os.path.join("static", "js", "csv_wizard.js"),
        )
        forbidden = ("mapModeCreate", "prefillCreateAssignmentFromCsv", "create_assignment")
        for name in names:
            path = os.path.join(os.path.dirname(__file__), "..", "..", "app", name)
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            for token in forbidden:
                self.assertNotIn(token, src, name)

    def test_csv_wizard_replaces_pdf_analyzer_script(self):
        root = os.path.join(os.path.dirname(__file__), "..", "..", "app")
        with open(os.path.join(root, "templates", "classes", "update_grade.html"), encoding="utf-8") as fh:
            template = fh.read()
        with open(os.path.join(root, "static", "js", "csv_wizard.js"), encoding="utf-8") as fh:
            script = fh.read()
        self.assertIn("js/csv_wizard.js') }}?v=23", template)
        self.assertFalse(os.path.exists(os.path.join(root, "static", "js", "pdf_analyzer.js")))
        for src in (template, script):
            self.assertNotIn("pdfInput", src)
            self.assertNotIn("uploadedPDFData", src)
            self.assertNotIn("analyzePDF", src)

    def test_add_student_flag_inputs_are_not_native_constraints(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "static", "js", "csv_wizard.js",
        )
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        marker = "function buildUnmatchedNameFlag("
        start = src.find(marker)
        self.assertGreaterEqual(start, 0)
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
        self.assertGreaterEqual(end, 0)
        body = src[start:end]
        self.assertNotIn(".required = true", body)
        self.assertNotIn("type = 'email'", body)

    def test_csv_wizard_has_no_add_student_notices(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "static", "js", "csv_wizard.js",
        )
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("addStudentNotices", src)
        self.assertNotIn(
            "who is now in this class. Correct the name in the sheet row to match.",
            src,
        )


class TestImportBlankCellActions(unittest.TestCase):
    def test_present_blank_is_clear_candidate_missing_key_is_not(self):
        grades = {"D1": "M", "D2": ""}
        to_save = []
        to_clear = []
        for lo_name, mark in grades.items():
            mark_s = "" if mark is None else str(mark).strip()
            if not mark_s:
                to_clear.append(lo_name)
            else:
                to_save.append(lo_name)
        self.assertEqual(to_save, ["D1"])
        self.assertEqual(to_clear, ["D2"])
        self.assertNotIn("D3", to_save)
        self.assertNotIn("D3", to_clear)


class TestParseGradeCsvRoute(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_unauthenticated_401(self):
        rv = self.client.post("/api/class/c1/parse-grade-csv")
        self.assertEqual(rv.status_code, 401)

    def test_valid_csv_matches_assignment(self):
        from app.routes import grading as r
        csv_text = (
            "Assignment,Quiz 1\n"
            "Date,2026-08-20\n"
            "\n"
            "Student Name,HW,D1\n"
            "Ada Lovelace,90,M\n"
        )
        sa = MagicMock()
        q = MagicMock()
        q.select.return_value = q
        q.eq.return_value = q
        q.execute.return_value = MagicMock(data=[{"id": "asg-1", "name": "Quiz 1"}])
        sa.table.return_value = q

        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"

        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(
                    Course, "get_learning_objectives", return_value=[{"vendor_code": "D1"}]
                ):
            rv = self.client.post(
                "/api/class/c1/parse-grade-csv",
                data={"file": (io.BytesIO(csv_text.encode("utf-8")), "quiz.csv")},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 200)
        body = rv.get_json()
        self.assertTrue(body.get("success"))
        self.assertEqual(body.get("matched_assignment_id"), "asg-1")
        self.assertEqual(body["data"]["extraction_path"], "csv")
        self.assertEqual(body["data"]["students"][0]["grades"], {"D1": "M"})

    def test_three_metadata_row_csv_parses_header_and_students(self):
        from app.routes import grading as r
        csv_text = (
            "Assignment,Quiz 1\n"
            "Date,2026-08-20\n"
            "NOTE,This is a blank template. Grade columns are intentionally empty. "
            "Re-uploading this file will CLEAR any grades already entered for this assignment.\n"
            "\n"
            "Student Name,HW,D1\n"
            "Ada Lovelace,90,M\n"
            "Doe Jane,0,\n"
        )
        sa = MagicMock()
        q = MagicMock()
        q.select.return_value = q
        q.eq.return_value = q
        q.execute.return_value = MagicMock(data=[{"id": "asg-1", "name": "Quiz 1"}])
        sa.table.return_value = q

        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"

        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(
                    Course, "get_learning_objectives", return_value=[{"vendor_code": "D1"}]
                ):
            rv = self.client.post(
                "/api/class/c1/parse-grade-csv",
                data={"file": (io.BytesIO(csv_text.encode("utf-8")), "quiz.csv")},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 200)
        body = rv.get_json()
        self.assertTrue(body.get("success"))
        self.assertEqual(body["data"]["assignment_name"], "Quiz 1")
        self.assertEqual(body["data"]["learning_objectives"], ["D1"])
        students = body["data"]["students"]
        self.assertEqual(len(students), 2)
        self.assertEqual(students[0]["name"], "Ada Lovelace")
        self.assertEqual(students[0]["grades"], {"D1": "M"})
        self.assertEqual(students[0]["homework_pct"], "90")
        self.assertEqual(students[1]["name"], "Doe Jane")
        self.assertEqual(students[1]["grades"], {"D1": ""})
        self.assertEqual(students[1]["homework_pct"], "0")
        self.assertFalse(any("NOTE" in (s.get("name") or "").upper() for s in students))

    def test_malformed_csv_returns_400(self):
        from app.routes import grading as r
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(
                    Course, "get_learning_objectives", return_value=[{"vendor_code": "D1"}]
                ):
            rv = self.client.post(
                "/api/class/c1/parse-grade-csv",
                data={"file": (io.BytesIO(b"nope"), "bad.csv")},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 400)
        self.assertIn("not a Clarity gradesheet export", rv.get_json().get("error") or "")

    def _post_grade_csv(self, csv_text, assignment_rows, owns=True):
        from app.routes import grading as r
        sa = MagicMock()
        q = MagicMock()
        q.select.return_value = q
        q.eq.return_value = q
        q.execute.return_value = MagicMock(data=assignment_rows)
        sa.table.return_value = q
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=owns), \
                unittest.mock.patch.object(
                    Course, "get_learning_objectives", return_value=[{"vendor_code": "D1"}]
                ):
            return self.client.post(
                "/api/class/c1/parse-grade-csv",
                data={"file": (io.BytesIO(csv_text.encode("utf-8")), "quiz.csv")},
                headers={"X-CSRF-Token": "test-csrf"},
            )

    def test_spaced_title_matches_stored_name(self):
        csv_text = (
            "Assignment,quiz   1\n"
            "Date,2026-08-20\n"
            "\n"
            "Student Name,HW,D1\n"
            "Ada Lovelace,90,M\n"
        )
        rv = self._post_grade_csv(csv_text, [{"id": "asg-1", "name": "Quiz 1"}])
        self.assertEqual(rv.status_code, 200)
        body = rv.get_json()
        self.assertEqual(body.get("matched_assignment_id"), "asg-1")
        self.assertEqual(body.get("matched_assignment_title"), "Quiz 1")
        self.assertEqual(body.get("assignment_match"), "matched")

    def test_unknown_title_400_names_the_file_title(self):
        csv_text = (
            "Assignment,Test\n"
            "Date,2026-08-20\n"
            "\n"
            "Student Name,HW,D1\n"
            "Ada Lovelace,90,M\n"
        )
        rv = self._post_grade_csv(csv_text, [])
        self.assertEqual(rv.status_code, 400)
        body = rv.get_json()
        self.assertEqual(body.get("error"), "No assignment named 'Test' in this class.")
        self.assertNotIn("students", body)

    def test_unknown_title_with_markup_and_quote_is_plain_text(self):
        title = 'Quiz <b>x</b> "1"'
        csv_text = (
            f"Assignment,{title}\n"
            "Date,2026-08-20\n"
            "\n"
            "Student Name,HW,D1\n"
            "Ada Lovelace,90,M\n"
        )
        rv = self._post_grade_csv(csv_text, [])
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(
            rv.get_json().get("error"),
            "No assignment named 'Quiz <b>x</b> \"1\"' in this class.",
        )

    def test_duplicate_normalized_titles_are_ambiguous(self):
        csv_text = (
            "Assignment,Quiz 1\n"
            "Date,2026-08-20\n"
            "\n"
            "Student Name,HW,D1\n"
            "Ada Lovelace,90,M\n"
        )
        rv = self._post_grade_csv(csv_text, [
            {"id": "asg-1", "name": "Quiz 1"},
            {"id": "asg-2", "name": "quiz  1"},
        ])
        self.assertEqual(rv.status_code, 200)
        body = rv.get_json()
        self.assertIsNone(body.get("matched_assignment_id"))
        self.assertEqual(body.get("assignment_match"), "ambiguous")
        self.assertEqual(body["data"]["students"][0]["grades"], {"D1": "M"})

    def test_short_missing_empty_and_undated_files_use_export_message(self):
        sheets = [
            "Name,Score\nAda,M\n",
            "Student Name,HW,D1\nAda,90,M\n\n\n",
            "Assignment,\nDate,2026-08-20\n\nStudent Name,HW,D1\nAda,90,M\n",
            "Assignment,Quiz 1\nWhen,2026-08-20\n\nStudent Name,HW,D1\nAda,90,M\n",
        ]
        for sheet in sheets:
            rv = self._post_grade_csv(sheet, [{"id": "asg-1", "name": "Quiz 1"}])
            self.assertEqual(rv.status_code, 400)
            self.assertIn("not a Clarity gradesheet export", rv.get_json().get("error") or "")
            self.assertNotIn("students", rv.get_json())

    def test_unowned_class_403(self):
        rv = self._post_grade_csv("Assignment,Quiz 1\n", [], owns=False)
        self.assertEqual(rv.status_code, 403)


class TestAllowedLoIdsAssignmentScoping(unittest.TestCase):
    """_allowed_lo_ids_for_grading must scope to assignment-linked LOs only.

    Before fix: returned Course.get_lo_ids_for_class regardless of assignment_id,
    so grading an LO not linked to the assignment was silently accepted and written.
    After fix: when assignment_id is present, only LOs in assignment_objectives for
    that assignment are returned; an unlinked LO is dropped before any DB write.
    """

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def _save_grades_call(self, lo_id, linked_lo_ids, hw_map=None):
        """Post save_grades for a single lo_id; returns (response, captured_rows).

        linked_lo_ids controls what Course.get_assignment_lo_ids returns —
        i.e. which LOs are actually linked to the assignment.
        """
        from app.routes import grading as r
        if hw_map is None:
            hw_map = {"stu-1": 80}   # recorded score so HW gate passes
        captured_rows = []
        exec_m = MagicMock()
        q = MagicMock()
        q.upsert.side_effect = lambda rows, **kw: captured_rows.extend(rows) or exec_m

        sa = MagicMock()
        sa.table.return_value = q

        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["csrf_token"] = "test-csrf"

        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_assignment_belongs_to_class", return_value=True), \
                unittest.mock.patch.object(r, "_class_auto_convert_m_enabled", return_value=False), \
                unittest.mock.patch.object(
                    r, "_enrolled_student_ids_for_class", return_value={"stu-1"}
                ), \
                unittest.mock.patch.object(
                    Course, "get_assignment_lo_ids", return_value=linked_lo_ids
                ), \
                unittest.mock.patch.object(
                    Homework, "get_hw_scores_map_for_assignment", return_value=hw_map
                ):
            rv = self.client.post(
                "/api/class/class-1/save-grades",
                json={"assignment_id": "asg-1", "grades": {"stu-1|" + lo_id: "M"}},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        return rv, captured_rows

    def test_linked_lo_is_accepted(self):
        """Grade for an LO that IS linked to the assignment is written."""
        rv, rows = self._save_grades_call(
            lo_id="lo-linked",
            linked_lo_ids=["lo-linked"],
        )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["learning_objective_id"], "lo-linked")

    def test_unlinked_lo_is_silently_dropped(self):
        """Grade for an LO in the class pool but NOT linked to the assignment
        must be silently dropped — response is 200 but no row is written."""
        rv, rows = self._save_grades_call(
            lo_id="lo-unlinked",
            linked_lo_ids=["lo-linked"],   # unlinked is absent here
        )
        # Route still succeeds (other cells may have been valid); the bad
        # cell is simply skipped rather than causing a 4xx.
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rows, [], "unlinked LO must not produce a DB write")

    def test_no_assignment_id_uses_full_class_pool(self):
        """Without an assignment_id, the full class LO pool is still allowed
        (unscoped grade entry path)."""
        from app.routes import grading as r
        captured_rows = []
        exec_m = MagicMock()
        q = MagicMock()
        q.upsert.side_effect = lambda rows, **kw: captured_rows.extend(rows) or exec_m
        sa = MagicMock()
        sa.table.return_value = q

        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["csrf_token"] = "test-csrf"

        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(
                    r, "_enrolled_student_ids_for_class", return_value={"stu-1"}
                ), \
                unittest.mock.patch.object(
                    Course, "get_lo_ids_for_class", return_value=["lo-any"]
                ):
            rv = self.client.post(
                "/api/class/class-1/save-grades",
                # No assignment_id — unscoped grade entry
                json={"grades": {"stu-1|lo-any": "M"}},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(len(captured_rows), 1)
        self.assertEqual(captured_rows[0]["learning_objective_id"], "lo-any")


class TestImportGradesRateLimit(unittest.TestCase):
    """api_import_grades must return 429 when the rate limiter is exhausted.

    Before fix: no @rate_limited decorator on api_import_grades — _rate_limit
    returning False had no effect, route body executed regardless.
    After fix: @rate_limited("import_grades", 20, 900) wraps the view; a
    False-returning _rate_limit short-circuits with 429 before any route logic
    runs.
    """

    def setUp(self):
        from app import create_app
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_returns_429_when_rate_limit_exhausted(self):
        """Patching _rate_limit to return False must produce a 429 with the
        standard rate-limit error message."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["csrf_token"] = "test-csrf"

        with unittest.mock.patch("app.helpers.rate_limit._rate_limit", return_value=False):
            rv = self.client.post(
                "/api/import-grades",
                json={"class_id": "c1"},
                headers={"X-CSRF-Token": "test-csrf"},
            )

        self.assertEqual(rv.status_code, 429)
        body = rv.get_json()
        self.assertFalse(body["success"])
        self.assertIn("Rate limit", body["error"])

    def test_route_proceeds_when_rate_limit_not_exhausted(self):
        """When the limiter passes, the request must not produce a 429.
        (It may fail for other reasons — missing data — but not rate-limiting.)"""
        from app.routes import grading as r
        sa = MagicMock()
        q = MagicMock()
        q.select.return_value = q
        q.eq.return_value = q
        q.in_.return_value = q
        q.execute.return_value = MagicMock(data=[])
        sa.table.return_value = q

        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["csrf_token"] = "test-csrf"

        with unittest.mock.patch("app.helpers.rate_limit._rate_limit", return_value=True), \
                unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch("app.helpers.lo_import.supabase_admin", sa):
            rv = self.client.post(
                "/api/import-grades",
                json={"class_id": "c1", "assignment_id": "a1", "students": []},
                headers={"X-CSRF-Token": "test-csrf"},
            )

        self.assertNotEqual(rv.status_code, 429)


class TestGradeChangeLog(unittest.TestCase):
    """Python writes DELETE audit rows. INSERT and UPDATE belong to the trigger."""

    def setUp(self):
        from app import create_app
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def _make_sa_mock(self, existing_grades=None):
        grade_rows = []
        log_rows = []
        exec_m = MagicMock()
        exec_m.data = list(existing_grades or [])

        def make_q():
            q = MagicMock()
            q.select.return_value = q
            q.eq.return_value = q
            q.in_.return_value = q
            q.limit.return_value = q
            q.order.return_value = q
            q.execute.return_value = exec_m
            q.upsert.return_value = q
            return q

        grades_q = make_q()
        grades_q.upsert.side_effect = lambda rows, **kw: grade_rows.extend(rows) or grades_q

        log_q = make_q()
        log_q.insert.side_effect = lambda rows: log_rows.extend(rows) or log_q

        fallback_q = make_q()

        sa = MagicMock()
        sa.table.side_effect = lambda name: (
            grades_q if name == "grades" else
            log_q if name == "grade_change_log" else
            fallback_q
        )
        return sa, grade_rows, log_rows

    def _login(self):
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["csrf_token"] = "test-csrf"

    def _save_patches(self, r, sa, hw_map):
        return (
            unittest.mock.patch.object(r, "supabase_admin", sa),
            unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True),
            unittest.mock.patch.object(r, "_assignment_belongs_to_class", return_value=True),
            unittest.mock.patch.object(r, "_class_auto_convert_m_enabled", return_value=False),
            unittest.mock.patch.object(r, "_enrolled_student_ids_for_class", return_value={"stu-1"}),
            unittest.mock.patch.object(Course, "get_assignment_lo_ids", return_value=["lo-x"]),
            unittest.mock.patch.object(
                Homework, "get_hw_scores_map_for_assignment", return_value=hw_map
            ),
        )

    def test_save_grades_empty_payload_writes_no_audit_row(self):
        from app.routes import grading as r
        sa, grade_rows, log_rows = self._make_sa_mock()
        self._login()
        patches = self._save_patches(r, sa, {})
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            rv = self.client.post(
                "/api/class/class-1/save-grades",
                json={"assignment_id": "asg-1", "grades": {}},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(grade_rows, [])
        self.assertEqual(log_rows, [])

    def test_save_grades_does_not_insert_grade_change_log(self):
        from app.routes import grading as r
        sa, grade_rows, log_rows = self._make_sa_mock()
        self._login()
        patches = self._save_patches(r, sa, {"stu-1": 80})
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            rv = self.client.post(
                "/api/class/class-1/save-grades",
                json={"assignment_id": "asg-1", "grades": {"stu-1|lo-x": "M"}},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 200, rv.get_json())
        self.assertEqual(len(grade_rows), 1)
        self.assertEqual(grade_rows[0]["last_modified_by"], "inst1")
        self.assertEqual(log_rows, [])

    def test_save_grades_upsert_failure_returns_500_after_one_attempt(self):
        from app.routes import grading as r
        upsert_calls = []
        grades_q = MagicMock()
        grades_q.select.return_value = grades_q
        grades_q.eq.return_value = grades_q
        grades_q.in_.return_value = grades_q
        grades_q.execute.return_value = MagicMock(data=[])

        def upsert(rows, **kwargs):
            upsert_calls.append(rows)
            failed = MagicMock()
            failed.execute.side_effect = RuntimeError("grades upsert failed")
            return failed

        grades_q.upsert.side_effect = upsert
        sa = MagicMock()
        sa.table.return_value = grades_q
        self._login()
        patches = self._save_patches(r, sa, {"stu-1": 80})
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            rv = self.client.post(
                "/api/class/class-1/save-grades",
                json={"assignment_id": "asg-1", "grades": {"stu-1|lo-x": "M"}},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 500)
        self.assertEqual(rv.get_json()["error"], "Could not save grades")
        self.assertEqual(len(upsert_calls), 1)

    def test_promoter_does_not_insert_grade_change_log(self):
        sa, grade_rows, log_rows = self._make_sa_mock(existing_grades=[{
            "student_id": "stu-1",
            "learning_objective_id": "lo-x",
            "assignment_id": "asg-1",
            "top_score": "M",
            "counts_for_mastery": False,
        }])
        with unittest.mock.patch("app.helpers.free_passes.supabase_admin", sa), \
                unittest.mock.patch.object(Course, "get_all_lo_ids_for_class", return_value=["lo-x"]):
            n = _promote_non_counting_masteries_for_student("class-1", "stu-1", changed_by="inst1")
        self.assertEqual(n, 1)
        self.assertEqual(grade_rows[0]["last_modified_by"], "inst1")
        self.assertEqual(log_rows, [])

    def test_allowed_operations_and_python_writer(self):
        from app.helpers import grade_log
        self.assertEqual(
            grade_log.GRADE_LOG_OPERATIONS,
            (grade_log.GRADE_LOG_INSERT, grade_log.GRADE_LOG_UPDATE, grade_log.GRADE_LOG_DELETE),
        )
        self.assertEqual(grade_log.GRADE_LOG_OPERATIONS, ("INSERT", "UPDATE", "DELETE"))
        sa, _grade_rows, log_rows = self._make_sa_mock()
        with unittest.mock.patch("app.helpers.grade_log.supabase_admin", sa):
            _log_grade_deletions(
                [{
                    "student_id": "stu-1",
                    "learning_objective_id": "lo-x",
                    "assignment_id": "asg-1",
                    "top_score": "R",
                    "second_score": None,
                    "counts_for_mastery": True,
                }],
                "class-1",
                "inst1",
            )
        ops = {row["operation"] for row in log_rows}
        self.assertEqual(ops, {grade_log.GRADE_LOG_DELETE})
        self.assertTrue(ops <= set(grade_log.GRADE_LOG_OPERATIONS))

    def test_constraint_violation_logs_error_with_operation(self):
        from app.routes import grading as r
        from app.helpers import grade_log

        class CheckViolation(Exception):
            def __init__(self):
                super().__init__("grade_change_log_operation_check")
                self.code = "23514"

        q = MagicMock()
        q.insert.return_value = q
        q.execute.side_effect = CheckViolation()
        sa = MagicMock()
        sa.table.return_value = q
        with unittest.mock.patch("app.helpers.grade_log.supabase_admin", sa), \
                unittest.mock.patch.object(grade_log.logger, "error") as err, \
                unittest.mock.patch.object(grade_log.logger, "warning") as warn, \
                unittest.mock.patch.object(r.logger, "error") as routes_err, \
                unittest.mock.patch.object(r.logger, "warning") as routes_warn:
            _log_grade_deletions(
                [{
                    "student_id": "stu-1",
                    "learning_objective_id": "lo-x",
                    "top_score": "R",
                    "second_score": None,
                    "counts_for_mastery": True,
                }],
                "class-1",
                "inst1",
            )
        warn.assert_not_called()
        routes_err.assert_not_called()
        routes_warn.assert_not_called()
        self.assertIn(grade_log.GRADE_LOG_DELETE, err.call_args[0])

    def test_missing_table_logs_warning_only(self):
        from app.routes import grading as r
        from app.helpers import grade_log

        class MissingTable(Exception):
            def __init__(self):
                super().__init__({"code": "42P01", "message": "grade_change_log does not exist"})

        q = MagicMock()
        q.insert.return_value = q
        q.execute.side_effect = MissingTable()
        sa = MagicMock()
        sa.table.return_value = q
        with unittest.mock.patch("app.helpers.grade_log.supabase_admin", sa), \
                unittest.mock.patch.object(grade_log.logger, "error") as err, \
                unittest.mock.patch.object(grade_log.logger, "warning") as warn, \
                unittest.mock.patch.object(r.logger, "error") as routes_err, \
                unittest.mock.patch.object(r.logger, "warning") as routes_warn:
            _log_grade_deletions(
                [{
                    "student_id": "stu-1",
                    "learning_objective_id": "lo-x",
                    "top_score": "R",
                    "second_score": None,
                    "counts_for_mastery": True,
                }],
                "class-1",
                "inst1",
            )
        err.assert_not_called()
        routes_err.assert_not_called()
        warn.assert_called()
        routes_warn.assert_not_called()


class TestFreePassAllowance(unittest.TestCase):
    def _post(self, path, rows):
        from app.routes import grading as r
        log = []

        class _PassQuery:
            def __init__(self):
                self.op = None
                self.payload = None

            def select(self, *args, **kwargs):
                self.op = "select"
                return self

            def insert(self, row):
                self.op = "insert"
                self.payload = row
                return self

            def update(self, row):
                self.op = "update"
                self.payload = row
                return self

            def eq(self, column, value):
                return self

            def execute(self):
                log.append({"op": self.op, "payload": self.payload})
                data = rows if self.op == "select" else []
                return unittest.mock.MagicMock(data=data)

        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "supabase_admin") as sa, \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_student_enrolled_in_class", return_value=True):
            sa.table.side_effect = lambda name: _PassQuery()
            rv = client.post(
                path,
                json={"student_id": "stu-1"},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        return rv, log

    def test_first_use_inserts_one_and_returns_one_less_than_the_allowance(self):
        from app.helpers.free_passes import FREE_PASSES_ALLOWED
        rv, log = self._post("/api/class/c1/use_pass", [])
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.get_json()["passes_remaining"], FREE_PASSES_ALLOWED - 1)
        inserted = [entry for entry in log if entry["op"] == "insert"]
        self.assertEqual(len(inserted), 1)
        self.assertEqual(inserted[0]["payload"]["passes_used"], 1)
        self.assertEqual(
            [entry for entry in log if entry["op"] == "update"],
            [],
        )

    def test_use_at_the_limit_returns_400_and_does_not_update(self):
        from app.helpers.free_passes import FREE_PASSES_ALLOWED
        rv, log = self._post(
            "/api/class/c1/use_pass",
            [{"id": "fp1", "passes_used": FREE_PASSES_ALLOWED}],
        )
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(rv.get_json()["error"], "No passes remaining")
        self.assertEqual(
            [entry for entry in log if entry["op"] == "update"],
            [],
        )

    def test_return_from_one_used_restores_the_allowance(self):
        from app.helpers.free_passes import FREE_PASSES_ALLOWED
        rv, log = self._post(
            "/api/class/c1/return_pass",
            [{"id": "fp1", "passes_used": 1}],
        )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.get_json()["passes_remaining"], FREE_PASSES_ALLOWED)
        updated = [entry for entry in log if entry["op"] == "update"]
        self.assertEqual(len(updated), 1)
        self.assertEqual(updated[0]["payload"], {"passes_used": 0})


def _page_without_csrf(page):
    """Return page HTML with the meta and hidden CSRF token values removed."""
    stripped = page
    for marker in ('name="csrf-token" content="', 'name="csrf_token" value="'):
        start = stripped.find(marker)
        if start == -1:
            continue
        value_start = start + len(marker)
        value_end = stripped.find('"', value_start)
        stripped = stripped[:value_start] + stripped[value_end:]
    return stripped


class TestUploadPageReads(unittest.TestCase):
    CLASS_ID = "class-up"
    EMPTY_ID = "class-up-empty"
    NONE_ID = "class-up-none"
    CALLS = [
        "classes", "classes", "enrollments", "assignments",
    ]
    NO_GRADE_CALLS = [
        "classes", "classes", "enrollments", "assignments",
    ]

    def _class_row(self, class_id, name, instructor_id="inst-1"):
        return {
            "id": class_id, "instructor_id": instructor_id, "name": name,
            "semester": "Fall", "days": "MWF", "section_number": "1",
            "auto_convert_m": False, "is_online": False,
        }

    def _objective(self, lo_id, class_id, vendor_code, name):
        return {
            "id": lo_id, "class_id": class_id, "vendor_code": vendor_code,
            "name": name, "description": "%s text" % name, "required_ms": 2,
        }

    def _link(self, assignment_id, lo_id, vendor_code):
        return {
            "assignment_id": assignment_id,
            "learning_objective_id": lo_id,
            "assignments": {"class_id": self.CLASS_ID},
            "learning_objectives": {
                "id": lo_id, "vendor_code": vendor_code, "description": "%s text" % vendor_code,
            },
        }

    def _assignment(self, assignment_id, name, homework_group, created_at, links, assignment_type, class_id=None):
        return {
            "id": assignment_id, "class_id": class_id or self.CLASS_ID, "name": name,
            "homework_group": homework_group, "date_returned": "2026-10-17",
            "revision_due": "2026-10-24", "created_at": created_at,
            "assignment_type": assignment_type, "assignment_objectives": links,
        }

    def _enrollment(self, class_id, enrollment_id, student_id, muted, profiles):
        return {
            "id": enrollment_id, "class_id": class_id, "student_id": student_id,
            "muted": muted, "profiles": profiles,
        }

    def _profile(self, student_id, full_name, role="student"):
        return {
            "id": student_id, "full_name": full_name,
            "role": role, "email": "%s@example.edu" % student_id,
        }

    def _store(self):
        link_b = self._link("asg-linked", "lo-1", "B2")
        link_a = self._link("asg-linked", "lo-2", "A1")
        return {
            "classes": [self._class_row(self.CLASS_ID, "Algebra")],
            "learning_objectives": [
                self._objective("lo-1", self.CLASS_ID, "B2", "Beta"),
                self._objective("lo-2", self.CLASS_ID, "A1", "Alpha"),
                self._objective("lo-9", self.CLASS_ID, "C3", "Gamma"),
            ],
            "enrollments": [
                self._enrollment(
                    self.CLASS_ID, "enr-0-ghost", "stu-ghost", False, None,
                ),
                self._enrollment(
                    self.CLASS_ID, "enr-1-zoe", "stu-zoe", False,
                    self._profile("stu-zoe", "Zoe, Ann"),
                ),
                self._enrollment(
                    self.CLASS_ID, "enr-2-ada", "stu-ada", False,
                    self._profile("stu-ada", "Ada, Ann"),
                ),
                self._enrollment(
                    self.CLASS_ID, "enr-2b-ada", "stu-ada", False,
                    self._profile("stu-ada", "Ada, Ann"),
                ),
                self._enrollment(
                    self.CLASS_ID, "enr-3-mute", "stu-mute", True,
                    self._profile("stu-mute", "Mute, Mo"),
                ),
                self._enrollment(
                    self.CLASS_ID, "enr-4-blank", "stu-blank", False,
                    self._profile("stu-blank", "  "),
                ),
                self._enrollment(
                    self.CLASS_ID, "enr-5-null", "stu-null", False,
                    self._profile("stu-null", None),
                ),
                self._enrollment(
                    self.CLASS_ID, "enr-6-pad", "stu-pad", False,
                    self._profile(" stu-pad ", "Pad, Pat"),
                ),
                self._enrollment(
                    self.CLASS_ID, "enr-7-quinn", "stu-quinn", False,
                    self._profile("stu-quinn", "Quinn, Pat", role="instructor"),
                ),
                self._enrollment(
                    self.CLASS_ID, "enr-8-list", "stu-lee", False,
                    [
                        self._profile("stu-lee", "Lee, Ann"),
                        self._profile("stu-skip", "Skip, Sue"),
                    ],
                ),
            ],
            "grades": [{
                "id": "g-ada", "student_id": "stu-ada",
                "learning_objective_id": "lo-2", "top_score": "ZQ",
                "second_score": None, "counts_for_mastery": True,
            }],
            "assignment_objectives": [link_b, link_a],
            "assignments": [
                self._assignment(
                    "asg-bare", "Bare Exam", "", "2026-10-02", [], "exam",
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
        from app.routes import grading as r
        calls = []
        admin = _class_detail_client(store, calls, [])
        app = create_app()
        app.config["TESTING"] = True
        captured = {}

        def grab(sender, template, context, **kwargs):
            if getattr(template, "name", "") == "classes/update_grade.html":
                captured["context"] = context

        template_rendered.connect(grab, app)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst-1"
            sess["role"] = "instructor"
        try:
            with unittest.mock.patch.object(r, "supabase_admin", admin), \
                    unittest.mock.patch.object(models, "supabase_admin", admin), \
                    unittest.mock.patch("app.helpers.class_data.supabase_admin", admin), \
                    unittest.mock.patch("app.helpers.class_access.supabase_admin", admin):
                response = client.get("/class/%s/update_grade" % class_id)
        finally:
            template_rendered.disconnect(grab, app)
        return response, calls, captured.get("context")

    def _roster_json(self, page):
        import json
        raw = page.split('id="classRosterJson">', 1)[1].split("</script>", 1)[0]
        return raw, json.loads(raw)

    def _assignments_json(self, page):
        import json
        raw = page.split('id="assignmentsJson">', 1)[1].split("</script>", 1)[0]
        return json.loads(raw)

    def _assignment_fields(self, assignment):
        return {
            "id": assignment["id"],
            "name": assignment["name"],
            "homework_group": assignment.get("homework_group"),
            "assignment_type": assignment.get("assignment_type"),
            "vendor_codes": [
                ((link.get("learning_objectives") or {}).get("vendor_code"))
                for link in (assignment.get("assignment_objectives") or [])
            ],
        }

    def _options(self, page):
        import re
        return re.findall(r'<option value="([^"]+)">([^<]+)</option>', page)

    def test_roster_keeps_muted_and_stored_names_and_ignores_grades(self):
        import json
        response, calls, context = self._page(self._store(), self.CLASS_ID)
        page = response.get_data(as_text=True)
        expected_roster = [
            {"id": "stu-ada", "full_name": "Ada, Ann"},
            {"id": "stu-ada", "full_name": "Ada, Ann"},
            {"id": "stu-lee", "full_name": "Lee, Ann"},
            {"id": "stu-mute", "full_name": "Mute, Mo"},
            {"id": "stu-pad", "full_name": "Pad, Pat"},
            {"id": "stu-quinn", "full_name": "Quinn, Pat"},
            {"id": "stu-zoe", "full_name": "Zoe, Ann"},
            {"id": "stu-blank", "full_name": "  "},
            {"id": "stu-null", "full_name": ""},
        ]
        expected_assignments = [
            {
                "id": "asg-linked", "name": "Linked Quiz", "homework_group": "HW 1",
                "assignment_type": "mastery_opp", "vendor_codes": ["A1", "B2"],
            },
            {
                "id": "asg-bare", "name": "Bare Exam", "homework_group": "",
                "assignment_type": "exam", "vendor_codes": [],
            },
        ]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, self.CALLS)
        self.assertEqual(context["class_name"], "Algebra")
        self.assertIn(">Upload Grades</h1>", page)
        self.assertNotIn("Algebra", page)
        self.assertNotIn("Unnamed student", page)
        self.assertNotIn("stu-ghost", page)
        self.assertNotIn("stu-skip", page)
        self.assertNotIn("Skip, Sue", page)
        self.assertNotIn("ZQ", _page_without_csrf(page))
        self.assertNotIn("Gamma", page)
        raw_roster, roster = self._roster_json(page)
        self.assertEqual(roster, expected_roster)
        self.assertEqual(raw_roster, json.dumps(expected_roster, sort_keys=True))
        self.assertEqual(list(roster[0].keys()), ["full_name", "id"])
        self.assertEqual(
            [self._assignment_fields(row) for row in self._assignments_json(page)],
            expected_assignments,
        )
        self.assertEqual(
            [self._assignment_fields(row) for row in context["assignments"]],
            expected_assignments,
        )
        self.assertEqual(self._options(page), [
            ("asg-linked", "Linked Quiz"),
            ("asg-bare", "Bare Exam"),
        ])

    def test_no_enrollments_has_an_empty_roster(self):
        link = self._link("asg-linked", "lo-2", "A1")
        store = {
            "classes": [self._class_row(self.EMPTY_ID, "Algebra")],
            "learning_objectives": [
                self._objective("lo-2", self.EMPTY_ID, "A1", "Alpha"),
            ],
            "enrollments": [],
            "grades": [],
            "assignment_objectives": [link],
            "assignments": [self._assignment(
                "asg-linked", "Linked Quiz", "HW 1", "2026-10-01", [link], "mastery_opp",
                self.EMPTY_ID,
            )],
        }
        response, calls, context = self._page(store, self.EMPTY_ID)
        page = response.get_data(as_text=True)
        raw_roster, roster = self._roster_json(page)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, self.NO_GRADE_CALLS)
        self.assertEqual(roster, [])
        self.assertEqual(raw_roster, "[]")
        self.assertEqual(context["roster"], [])
        self.assertNotIn("grades", calls)

    def test_no_objectives_skips_the_grades_read(self):
        store = {
            "classes": [self._class_row(self.NONE_ID, "Algebra")],
            "learning_objectives": [],
            "enrollments": [self._enrollment(
                self.NONE_ID, "enr-ada", "stu-ada", False,
                self._profile("stu-ada", "Ada, Ann"),
            )],
            "grades": [],
            "assignment_objectives": [],
            "assignments": [self._assignment(
                "asg-bare", "Bare Exam", "", "2026-10-02", [], "exam",
                self.NONE_ID,
            )],
        }
        response, calls, context = self._page(store, self.NONE_ID)
        page = response.get_data(as_text=True)
        _raw, roster = self._roster_json(page)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, self.NO_GRADE_CALLS)
        self.assertEqual(calls.count("learning_objectives"), 0)
        self.assertEqual(roster, [{"id": "stu-ada", "full_name": "Ada, Ann"}])
        self.assertEqual(context["assignments"][0]["assignment_objectives"], [])
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


class TestSpeedGraderReads(unittest.TestCase):
    CLASS_ID = "class-sg"
    MUTED_ID = "class-sg-muted"
    EMPTY_ID = "class-sg-empty"
    NONE_ID = "class-sg-none"
    CALLS = [
        "classes", "classes", "enrollments", "assignments",
        "free_passes", "learning_objectives",
    ]
    MUTED_CALLS = [
        "classes", "classes", "enrollments", "assignments", "learning_objectives",
    ]
    EMPTY_CALLS = [
        "classes", "classes", "enrollments", "assignments", "learning_objectives",
    ]
    NONE_CALLS = [
        "classes", "classes", "enrollments", "assignments",
        "free_passes", "learning_objectives",
    ]

    def _class_row(self, class_id, name, auto_convert_m=False, instructor_id="inst-1"):
        return {
            "id": class_id, "instructor_id": instructor_id, "name": name,
            "semester": "Fall", "days": "MWF", "section_number": "1",
            "auto_convert_m": auto_convert_m, "is_online": False,
        }

    def _objective(self, lo_id, class_id, vendor_code, name):
        return {
            "id": lo_id, "class_id": class_id, "vendor_code": vendor_code,
            "name": name, "description": "%s text" % name, "required_ms": 2,
        }

    def _link(self, assignment_id, lo_id):
        return {
            "assignment_id": assignment_id,
            "learning_objective_id": lo_id,
            "assignments": {"class_id": self.CLASS_ID},
            "learning_objectives": {
                "id": lo_id, "vendor_code": lo_id, "description": "",
            },
        }

    def _assignment(self, assignment_id, name, homework_group, created_at, links, assignment_type, class_id=None):
        return {
            "id": assignment_id, "class_id": class_id or self.CLASS_ID, "name": name,
            "homework_group": homework_group, "date_returned": "2026-10-17",
            "revision_due": "2026-10-24", "created_at": created_at,
            "assignment_type": assignment_type, "assignment_objectives": links,
        }

    def _enrollment(self, class_id, enrollment_id, student_id, muted, profiles):
        return {
            "id": enrollment_id, "class_id": class_id, "student_id": student_id,
            "muted": muted, "profiles": profiles,
        }

    def _profile(self, student_id, full_name):
        return {
            "id": student_id, "full_name": full_name,
            "role": "student", "email": "student@example.edu",
        }

    def _store(self, auto_convert_m=False):
        link_b = self._link("asg-linked", "lo-1")
        link_a = self._link("asg-linked", "lo-2")
        return {
            "classes": [self._class_row(self.CLASS_ID, "Speed Class", auto_convert_m)],
            "learning_objectives": [
                self._objective("lo-1", self.CLASS_ID, "B2", "Beta"),
                self._objective("lo-2", self.CLASS_ID, "A1", "Alpha"),
                self._objective("lo-9", self.CLASS_ID, "C3", "Gamma"),
            ],
            "enrollments": [
                self._enrollment(self.CLASS_ID, "enr-0-ghost", "stu-ghost", False, None),
                self._enrollment(
                    self.CLASS_ID, "enr-1-bea", "stu-bea", False,
                    self._profile("stu-bea", "Bea, Bo"),
                ),
                self._enrollment(
                    self.CLASS_ID, "enr-2-ada", "stu-ada", False,
                    self._profile("stu-ada", "Ada, Ann"),
                ),
                self._enrollment(
                    self.CLASS_ID, "enr-3-mute", "stu-mute", True,
                    self._profile("stu-mute", "Mute, Mo"),
                ),
                self._enrollment(
                    self.CLASS_ID, "enr-4-pad", "stu-pad", False,
                    self._profile(" stu-pad ", "Pad, Pat"),
                ),
                self._enrollment(
                    self.CLASS_ID, "enr-5-blank", "stu-blank", False,
                    self._profile("stu-blank", "  "),
                ),
            ],
            "grades": [{
                "id": "g-ada", "student_id": "stu-ada",
                "learning_objective_id": "lo-2", "top_score": "ZQ",
                "second_score": None, "counts_for_mastery": True,
            }],
            "free_passes": [
                {"class_id": self.CLASS_ID, "student_id": "stu-bea", "passes_used": 1},
                {"class_id": self.CLASS_ID, "student_id": "stu-mute", "passes_used": 2},
            ],
            "assignment_objectives": [link_b, link_a],
            "assignments": [
                self._assignment("asg-bare", "Bare Exam", "", "2026-10-02", [], "exam"),
                self._assignment(
                    "asg-linked", "Linked Quiz", "HW 1", "2026-10-01",
                    [link_b, link_a], "mastery_opp",
                ),
            ],
        }

    def _page(self, store, class_id):
        from flask import template_rendered
        from app import models
        from app.routes import grading as r
        calls = []
        admin = _class_detail_client(store, calls, [])
        app = create_app()
        app.config["TESTING"] = True
        captured = {}

        def grab(sender, template, context, **kwargs):
            if getattr(template, "name", "") == "classes/class_speed_grader.html":
                captured["context"] = context

        template_rendered.connect(grab, app)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst-1"
            sess["role"] = "instructor"
        try:
            with unittest.mock.patch.object(r, "supabase_admin", admin), \
                    unittest.mock.patch.object(models, "supabase_admin", admin), \
                    unittest.mock.patch("app.helpers.free_passes.supabase_admin", admin), \
                    unittest.mock.patch("app.helpers.class_data.supabase_admin", admin), \
                    unittest.mock.patch("app.helpers.class_access.supabase_admin", admin):
                response = client.get("/class/%s/speed_grader" % class_id)
        finally:
            template_rendered.disconnect(grab, app)
        return response, calls, captured.get("context")

    def _student_fields(self, students):
        return [
            {
                "id": student.get("id"),
                "name": student.get("name"),
                "passes_remaining": student.get("passes_remaining"),
            }
            for student in students
        ]

    def _headers(self, page):
        import re
        return re.findall(
            r'data-lo="([^"]*)" data-lo-id="([^"]*)" data-lo-subtitle="([^"]*)"',
            page,
        )

    def _vendor_codes(self, page):
        import re
        return re.findall(
            r'text-xs font-bold text-slate-800 dark:text-white">([^<]*)</div>',
            page,
        )

    def _assignment_json(self, page):
        import json
        raw = page.split('id="assignmentData" type="application/json">', 1)[1]
        raw = raw.split("</script>", 1)[0]
        return json.loads(raw)

    def _assignment_fields(self, assignment):
        return {
            "id": assignment["id"],
            "name": assignment["name"],
            "homework_group": assignment.get("homework_group"),
            "assignment_type": assignment.get("assignment_type"),
            "learning_objective_ids": [
                link.get("learning_objective_id")
                for link in (assignment.get("assignment_objectives") or [])
            ],
        }

    def _expected_students(self):
        return [
            {"id": "stu-ada", "name": "Ada, Ann", "passes_remaining": 2},
            {"id": "stu-bea", "name": "Bea, Bo", "passes_remaining": 1},
            {"id": " stu-pad ", "name": "Pad, Pat", "passes_remaining": 2},
            {"id": "stu-blank", "name": "Unnamed student", "passes_remaining": 2},
        ]

    def _expected_assignments(self):
        return [
            {
                "id": "asg-linked", "name": "Linked Quiz", "homework_group": "HW 1",
                "assignment_type": "mastery_opp",
                "learning_objective_ids": ["lo-1", "lo-2"],
            },
            {
                "id": "asg-bare", "name": "Bare Exam", "homework_group": "",
                "assignment_type": "exam", "learning_objective_ids": [],
            },
        ]

    def test_active_students_full_pool_and_convert_flag_off(self):
        import re
        response, calls, context = self._page(self._store(False), self.CLASS_ID)
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, self.CALLS)
        self.assertEqual(context["class_name"], "Speed Class")
        self.assertNotIn("Speed Class", page)
        self.assertEqual(context["auto_convert_m"], False)
        self.assertIn('id="autoConvertMJson">false<', page)
        js_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "static", "js", "class_speed_grader.js",
        )
        with open(js_path, encoding="utf-8") as handle:
            page_js = handle.read()
        self.assertIn(
            "const AUTO_CONVERT_M = JSON.parse(document.getElementById('autoConvertMJson').textContent);",
            page_js,
        )
        self.assertEqual(self._student_fields(context["students"]), self._expected_students())
        self.assertEqual(
            re.findall(r'data-student-row data-student-id="([^"]*)"', page),
            ["stu-ada", "stu-bea", " stu-pad ", "stu-blank"],
        )
        self.assertIn("Unnamed student", page)
        self.assertNotIn("Mute, Mo", page)
        self.assertNotIn("stu-ghost", page)
        self.assertNotIn("ZQ", _page_without_csrf(page))
        self.assertEqual(
            [(lo["id"], lo["name"], lo["vendor_code"]) for lo in context["lo_names"]],
            [("lo-2", "Alpha", "A1"), ("lo-1", "Beta", "B2"), ("lo-9", "Gamma", "C3")],
        )
        self.assertEqual(self._headers(page), [
            ("Alpha", "lo-2", "Alpha"),
            ("Beta", "lo-1", "Beta"),
            ("Gamma", "lo-9", "Gamma"),
        ])
        self.assertEqual(self._vendor_codes(page), ["A1", "B2", "C3"])
        self.assertEqual(
            [self._assignment_fields(row) for row in self._assignment_json(page)],
            self._expected_assignments(),
        )
        self.assertEqual(
            [self._assignment_fields(row) for row in context["assignments"]],
            self._expected_assignments(),
        )

    def test_convert_flag_on_does_not_change_the_students(self):
        response, calls, context = self._page(self._store(True), self.CLASS_ID)
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, self.CALLS)
        self.assertEqual(context["auto_convert_m"], True)
        self.assertIn('id="autoConvertMJson">true<', page)
        self.assertEqual(self._student_fields(context["students"]), self._expected_students())

    def test_every_muted_enrollment_skips_free_passes(self):
        store = {
            "classes": [self._class_row(self.MUTED_ID, "Speed Class")],
            "learning_objectives": [
                self._objective("lo-2", self.MUTED_ID, "A1", "Alpha"),
            ],
            "enrollments": [self._enrollment(
                self.MUTED_ID, "enr-mute", "stu-mute", True,
                self._profile("stu-mute", "Mute, Mo"),
            )],
            "grades": [],
            "free_passes": [
                {"class_id": self.MUTED_ID, "student_id": "stu-mute", "passes_used": 2},
            ],
            "assignment_objectives": [],
            "assignments": [],
        }
        response, calls, context = self._page(store, self.MUTED_ID)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, self.MUTED_CALLS)
        self.assertEqual(context["students"], [])
        self.assertNotIn("free_passes", calls)

    def test_no_enrollments_skips_grades_and_free_passes(self):
        store = {
            "classes": [self._class_row(self.EMPTY_ID, "Speed Class")],
            "learning_objectives": [
                self._objective("lo-2", self.EMPTY_ID, "A1", "Alpha"),
            ],
            "enrollments": [],
            "grades": [],
            "free_passes": [],
            "assignment_objectives": [],
            "assignments": [self._assignment(
                "asg-bare", "Bare Exam", "", "2026-10-02", [], "exam", self.EMPTY_ID,
            )],
        }
        response, calls, context = self._page(store, self.EMPTY_ID)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, self.EMPTY_CALLS)
        self.assertEqual(context["students"], [])
        self.assertNotIn("grades", calls)
        self.assertNotIn("free_passes", calls)

    def test_no_objectives_still_reads_free_passes(self):
        store = {
            "classes": [self._class_row(self.NONE_ID, "Speed Class")],
            "learning_objectives": [],
            "enrollments": [self._enrollment(
                self.NONE_ID, "enr-ada", "stu-ada", False,
                self._profile("stu-ada", "Ada, Ann"),
            )],
            "grades": [],
            "free_passes": [],
            "assignment_objectives": [],
            "assignments": [],
        }
        response, calls, context = self._page(store, self.NONE_ID)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, self.NONE_CALLS)
        self.assertEqual(context["lo_names"], [])
        self.assertEqual(self._student_fields(context["students"]), [
            {"id": "stu-ada", "name": "Ada, Ann", "passes_remaining": 2},
        ])
        self.assertNotIn("grades", calls)

    def test_other_instructors_class_redirects_home(self):
        store = {
            "classes": [self._class_row(self.CLASS_ID, "Speed Class", instructor_id="inst-other")],
            "learning_objectives": [], "enrollments": [], "grades": [],
            "free_passes": [], "assignment_objectives": [], "assignments": [],
        }
        response, calls, context = self._page(store, self.CLASS_ID)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/instructor/dashboard", response.headers["Location"])
        self.assertEqual(calls, ["classes"])
        self.assertIsNone(context)
