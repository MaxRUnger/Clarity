"""
Test suite for the Capstone grading application.

Covers:
    - Grade model logic (normalize_score, is_mastered, get_priority)
    - Route helper functions (enrich_grade, organize_by_learning_objectives, normalize_profile)
    - Flask app factory / configuration
    - Route smoke tests (public pages, auth redirects)
"""
import sys
import os
import io
import re
import unittest
import unittest.mock
from unittest.mock import MagicMock

_mock_supabase = MagicMock()
_mock_supabase_admin = MagicMock()

sys.modules.setdefault('app.authentication', MagicMock(
    supabase=_mock_supabase,
    supabase_admin=_mock_supabase_admin,
))

from app.models import (
    Grade,
    Homework,
    MASTERY_GRADES,
)
from app.routes import (
    organize_by_learning_objectives,
    normalize_profile,
    DEFAULT_REQUIRED_MS,
    _aggregate_lo_grades,
    parse_blank_gradesheet_csv_text,
    parse_students_csv_text,
    _csv_format_hw_score,
    _gradesheet_csv_data_rows,
    _gradesheet_letter_map_from_rows,
    _build_gradesheet_csv_text,
    _BLANK_GRADESHEET_NOTE,
    _GRADESHEET_HEADER_ERROR,
    _enrolled_import_name_index,
    _lookup_enrolled_import_student_id,
    MAX_IMPORT_ROWS,
)
from app import create_app
from app.student_names import COMMA_REQUIRED, display_name, row_sort_key
from tests.test_student_names import CANVAS_ORDER


# ==========================================================================
# Grade Model Tests
# ==========================================================================

class TestGradeNormalizeScore(unittest.TestCase):
    """Tests for Grade.normalize_score — the core grading logic."""

    def test_none_returns_none(self):
        self.assertIsNone(Grade.normalize_score(None))

    # -- numeric conversions --
    def test_high_numeric_returns_M(self):
        self.assertEqual(Grade.normalize_score(95), 'M')
        self.assertEqual(Grade.normalize_score(70), 'M')

    def test_mid_numeric_returns_R(self):
        self.assertEqual(Grade.normalize_score(65), 'R')
        self.assertEqual(Grade.normalize_score(50), 'R')

    def test_low_numeric_returns_P(self):
        self.assertEqual(Grade.normalize_score(30), 'P')
        self.assertEqual(Grade.normalize_score(0), 'P')

    def test_numeric_string_treated_as_number(self):
        self.assertEqual(Grade.normalize_score('99'), 'M')
        self.assertEqual(Grade.normalize_score('55.5'), 'R')

    def test_float_boundary(self):
        self.assertEqual(Grade.normalize_score(69.9), 'R')
        self.assertEqual(Grade.normalize_score(70.0), 'M')

    # -- pass-through codes --
    def test_valid_codes_returned_as_is(self):
        for code in ('M', 'MR', 'R', 'RQ', 'P', 'X', 'A', 'I'):
            self.assertEqual(Grade.normalize_score(code), code)

    def test_codes_are_case_insensitive(self):
        self.assertEqual(Grade.normalize_score('m'), 'M')
        self.assertEqual(Grade.normalize_score('rq'), 'RQ')

    def test_whitespace_stripped(self):
        self.assertEqual(Grade.normalize_score('  M  '), 'M')

    def test_unrecognized_string_returns_none(self):
        self.assertIsNone(Grade.normalize_score('Z'))
        self.assertIsNone(Grade.normalize_score('hello'))


class TestGradeIsMastered(unittest.TestCase):
    """Tests for Grade.is_mastered — checks if both scores are 'M'."""

    def test_both_M_is_mastered(self):
        self.assertTrue(Grade.is_mastered('M', 'M'))

    def test_MR_counts_as_mastery_in_pair(self):
        self.assertTrue(Grade.is_mastered('MR', 'MR'))
        self.assertTrue(Grade.is_mastered('M', 'MR'))
        self.assertTrue(Grade.is_mastered('MR', 'M'))

    def test_only_top_M_not_mastered(self):
        self.assertFalse(Grade.is_mastered('M', 'R'))

    def test_only_second_M_not_mastered(self):
        self.assertFalse(Grade.is_mastered('R', 'M'))

    def test_neither_M_not_mastered(self):
        self.assertFalse(Grade.is_mastered('P', 'X'))


class TestGradeGetPriority(unittest.TestCase):
    """Tests for Grade.get_priority — used for score comparisons."""

    def test_known_grades_ordered_correctly(self):
        self.assertEqual(Grade.get_priority('M'), Grade.get_priority('MR'))
        self.assertGreater(Grade.get_priority('M'), Grade.get_priority('R'))
        self.assertGreater(Grade.get_priority('R'), Grade.get_priority('RQ'))
        self.assertGreater(Grade.get_priority('RQ'), Grade.get_priority('P'))
        self.assertGreater(Grade.get_priority('P'), Grade.get_priority('X'))
        self.assertGreater(Grade.get_priority('X'), Grade.get_priority('A'))
        self.assertEqual(Grade.get_priority('I'), Grade.get_priority('X'))

    def test_unknown_grade_returns_negative(self):
        self.assertEqual(Grade.get_priority('Z'), -1)
        self.assertEqual(Grade.get_priority(None), -1)


class TestHomeworkImportSheetColumn(unittest.TestCase):
    """Homework % columns on uploaded sheets are not learning objectives."""

    def test_is_import_sheet_hw_column(self):
        self.assertTrue(Homework.is_import_sheet_hw_column("HW"))
        self.assertTrue(Homework.is_import_sheet_hw_column("  HW%  "))
        self.assertTrue(Homework.is_import_sheet_hw_column("Homework"))
        self.assertTrue(Homework.is_import_sheet_hw_column("HW1"))
        self.assertFalse(Homework.is_import_sheet_hw_column("HW prev"))
        self.assertFalse(Homework.is_import_sheet_hw_column("EX1"))
        self.assertFalse(Homework.is_import_sheet_hw_column("A7"))

    def test_normalize_homework_group(self):
        # homework_group is a free-form grouping key: trim + collapse whitespace only.
        self.assertEqual(Homework.normalize_homework_group("Quiz 1"), "Quiz 1")
        self.assertEqual(Homework.normalize_homework_group("  Chapter  5   HW  "), "Chapter 5 HW")
        self.assertEqual(Homework.normalize_homework_group("EX1"), "EX1")
        self.assertIsNone(Homework.normalize_homework_group(""))
        self.assertIsNone(Homework.normalize_homework_group("   "))
        self.assertIsNone(Homework.normalize_homework_group(None))

    def test_canonicalize_homework_group_for_class_reuses_existing_casing(self):
        # If a differently-cased label already exists for this class, reuse
        # its exact casing so the two assignments share one HW group instead
        # of silently splitting into two ("Quiz 1" vs "quiz 1").
        with unittest.mock.patch.object(Homework, "normalize_homework_group", wraps=Homework.normalize_homework_group):
            mock_table = MagicMock()
            mock_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[{"homework_group": "Quiz 1"}, {"homework_group": "Unit 3"}]
            )
            with unittest.mock.patch("app.models.supabase_admin") as sa:
                sa.table.return_value = mock_table
                self.assertEqual(
                    Homework.canonicalize_homework_group_for_class("class-1", "quiz 1"),
                    "Quiz 1",
                )
                self.assertEqual(
                    Homework.canonicalize_homework_group_for_class("class-1", "  QUIZ   1  "),
                    "Quiz 1",
                )
                # A genuinely new label is returned as typed (trim/whitespace only).
                self.assertEqual(
                    Homework.canonicalize_homework_group_for_class("class-1", "Midterm"),
                    "Midterm",
                )

    def test_canonicalize_homework_group_for_class_handles_empty_and_lookup_errors(self):
        self.assertIsNone(Homework.canonicalize_homework_group_for_class("class-1", ""))
        self.assertIsNone(Homework.canonicalize_homework_group_for_class("class-1", None))
        with unittest.mock.patch("app.models.supabase_admin") as sa:
            sa.table.side_effect = Exception("boom")
            # Lookup failures shouldn't block assignment creation — fall back
            # to the normalized candidate as-is.
            self.assertEqual(
                Homework.canonicalize_homework_group_for_class("class-1", "Quiz 1"),
                "Quiz 1",
            )

    def test_parse_import_hw_pct(self):
        self.assertEqual(Homework.parse_import_hw_pct("85"), 85)
        self.assertEqual(Homework.parse_import_hw_pct(" 92.3% "), 92)
        self.assertEqual(Homework.parse_import_hw_pct(-1), -1)
        self.assertIsNone(Homework.parse_import_hw_pct("M"))
        self.assertIsNone(Homework.parse_import_hw_pct(""))
        self.assertIsNone(Homework.parse_import_hw_pct(None))

    def test_decide_hw_score_update_first_changed_and_unchanged(self):
        first = Homework.decide_hw_score_update(None, "80")
        self.assertEqual(first["action"], "first_set")
        self.assertEqual(first["score_pct"], 80)
        self.assertEqual(first["prev_score_pct"], 80)

        shifted = Homework.decide_hw_score_update(70, 85)
        self.assertEqual(shifted["action"], "shift_and_set")
        self.assertEqual(shifted["score_pct"], 85)
        self.assertEqual(shifted["prev_score_pct"], 70)

        same = Homework.decide_hw_score_update(70, 70)
        self.assertEqual(same, {"action": "unchanged"})

        pass_to_zero = Homework.decide_hw_score_update(-1, 0)
        self.assertEqual(pass_to_zero["action"], "shift_and_set")
        self.assertEqual(pass_to_zero["score_pct"], 0)
        self.assertEqual(pass_to_zero["prev_score_pct"], -1)

        pass_to_eighty = Homework.decide_hw_score_update(-1, 80)
        self.assertEqual(pass_to_eighty["action"], "shift_and_set")
        self.assertEqual(pass_to_eighty["score_pct"], 80)
        self.assertEqual(pass_to_eighty["prev_score_pct"], -1)

    def test_student_has_recorded_score(self):
        self.assertFalse(Homework.student_has_recorded_score({}, "stu-1"))
        self.assertFalse(Homework.student_has_recorded_score(None, "stu-1"))
        self.assertFalse(Homework.student_has_recorded_score({"stu-1": None}, "stu-1"))
        self.assertFalse(Homework.student_has_recorded_score({"stu-2": 80}, "stu-1"))
        self.assertTrue(Homework.student_has_recorded_score({"stu-1": 0}, "stu-1"))
        self.assertTrue(Homework.student_has_recorded_score({"stu-1": -1}, "stu-1"))
        self.assertTrue(Homework.student_has_recorded_score({"stu-1": 40}, "stu-1"))
        self.assertTrue(Homework.student_has_recorded_score({"stu-1": 80}, "stu-1"))


class TestHomeworkEligibilityThresholds(unittest.TestCase):
    """HW % rules: 65%+ in-class exam marks; 75%+ for M/MR; pass (-1) = exam only."""

    def test_exam_grade_eligible(self):
        self.assertFalse(Homework.is_exam_grade_eligible_hw_score(None))
        self.assertFalse(Homework.is_exam_grade_eligible_hw_score(64))
        self.assertTrue(Homework.is_exam_grade_eligible_hw_score(65))
        self.assertTrue(Homework.is_exam_grade_eligible_hw_score(100))
        self.assertTrue(Homework.is_exam_grade_eligible_hw_score(-1))

    def test_revision_to_m_eligible(self):
        self.assertFalse(Homework.is_revision_to_m_eligible_hw_score(None))
        self.assertFalse(Homework.is_revision_to_m_eligible_hw_score(-1))
        self.assertFalse(Homework.is_revision_to_m_eligible_hw_score(74))
        self.assertTrue(Homework.is_revision_to_m_eligible_hw_score(75))
        self.assertTrue(Homework.is_revision_to_m_eligible_hw_score(100))


# ==========================================================================
# Route Helper Tests
# ==========================================================================

class TestOrganizeByLearningObjectives(unittest.TestCase):
    """Tests for organize_by_learning_objectives — builds the LO summary."""

    def _build_data(self, grades_list):
        """Helper: single student with the given grades."""
        return [{'id': 'stu1', 'full_name': 'Test Student', 'grades': grades_list}]

    def test_student_with_2m(self):
        students = self._build_data([
            {'learning_objective_id': '10', 'top_score': 'M', 'second_score': 'M'},
        ])
        los = [{'id': '10', 'name': 'LO-A'}]
        result = organize_by_learning_objectives(students, los)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]['students_with_2m']), 1)
        self.assertEqual(len(result[0]['students_with_1m']), 0)

    def test_student_with_2m_via_MR(self):
        students = self._build_data([
            {'learning_objective_id': '10', 'top_score': 'MR', 'second_score': 'MR'},
        ])
        los = [{'id': '10', 'name': 'LO-A'}]
        result = organize_by_learning_objectives(students, los)
        self.assertEqual(len(result[0]['students_with_2m']), 1)

    def test_student_with_1m(self):
        students = self._build_data([
            {'learning_objective_id': '10', 'top_score': 'M', 'second_score': 'R'},
        ])
        los = [{'id': '10', 'name': 'LO-A'}]
        result = organize_by_learning_objectives(students, los)
        self.assertEqual(len(result[0]['students_with_1m']), 1)

    def test_student_with_0m(self):
        students = self._build_data([
            {'learning_objective_id': '10', 'top_score': 'P', 'second_score': 'X'},
        ])
        los = [{'id': '10', 'name': 'LO-A'}]
        result = organize_by_learning_objectives(students, los)
        self.assertEqual(len(result[0]['students_with_0m']), 1)

    def test_empty_students_list(self):
        los = [{'id': '10', 'name': 'LO-A'}]
        result = organize_by_learning_objectives([], los)
        self.assertEqual(result[0]['total_students'], 0)
        self.assertEqual(result[0]['students_with_2m'], [])

    def test_aggregate_lo_counts_M_and_MR(self):
        lo_lookup = {'1': {'id': '1', 'name': 'LO', 'vendor_code': 'L1'}}
        raw = [
            {'learning_objective_id': '1', 'top_score': 'M', 'learning_objectives': None},
            {'learning_objective_id': '1', 'top_score': 'MR', 'learning_objectives': None},
        ]
        out = _aggregate_lo_grades(raw, lo_lookup)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['m_count'], 2)
        self.assertEqual(out[0]['mr_count'], 1)
        self.assertTrue(out[0]['is_passed'])

    def test_multiple_los(self):
        students = self._build_data([
            {'learning_objective_id': '10', 'top_score': 'M', 'second_score': 'M'},
            {'learning_objective_id': '20', 'top_score': 'P', 'second_score': 'P'},
        ])
        los = [{'id': '10', 'name': 'LO-A'}, {'id': '20', 'name': 'LO-B'}]
        result = organize_by_learning_objectives(students, los)
        lo_a = next(lo for lo in result if lo['name'] == 'LO-A')
        lo_b = next(lo for lo in result if lo['name'] == 'LO-B')
        self.assertEqual(len(lo_a['students_with_2m']), 1)
        self.assertEqual(len(lo_b['students_with_0m']), 1)


class TestStoredCanvasNames(unittest.TestCase):
    def test_display_keeps_the_stored_string(self):
        self.assertEqual(display_name("Smith, Cali"), "Smith, Cali")

    def test_csv_keeps_last_comma_first(self):
        rows, warnings = parse_students_csv_text(
            'name,email\n"Juarez Salgado, Evelyn",evelyn@example.edu\n'
        )
        self.assertEqual(warnings, [])
        self.assertEqual(rows[0]["full_name"], "Juarez Salgado, Evelyn")

    def test_csv_without_a_comma_is_a_row_error(self):
        rows, warnings = parse_students_csv_text("name\nCali Smith\n")
        self.assertEqual(rows, [])
        self.assertTrue(any("Last, First" in warning for warning in warnings))

    def test_blank_full_name_sorts_last(self):
        rows = [
            {"full_name": "Zenith, Bob"},
            {"full_name": "Adams, Zoe"},
            {"full_name": ""},
        ]
        ordered = [row["full_name"] for row in sorted(rows, key=row_sort_key)]
        self.assertEqual(ordered, ["Adams, Zoe", "Zenith, Bob", ""])

    def test_organize_buckets_keep_stored_names(self):
        students = [
            {
                "id": "1",
                "full_name": "Zenith, Bob",
                "grades": [
                    {"learning_objective_id": "10", "top_score": "M", "second_score": "M"},
                ],
            },
            {
                "id": "2",
                "full_name": "Adams, Zoe",
                "grades": [
                    {"learning_objective_id": "10", "top_score": "M", "second_score": "M"},
                ],
            },
        ]
        los = [{"id": "10", "name": "LO-A"}]
        result = organize_by_learning_objectives(students, los)
        bucket = result[0]["students_with_2m"]
        self.assertEqual([s["name"] for s in bucket], ["Adams, Zoe", "Zenith, Bob"])
        self.assertEqual(bucket[0]["full_name"], "Adams, Zoe")
        self.assertEqual(bucket[1]["full_name"], "Zenith, Bob")

    def test_gradesheet_download_quotes_the_stored_name(self):
        students = [{"id": "stu-1", "full_name": "Smith, Cali"}]
        data_rows = _gradesheet_csv_data_rows(students, [], {}, {})
        text = _build_gradesheet_csv_text("Quiz 1", "2026-08-20", [], data_rows, False)
        self.assertIn('"Smith, Cali"', text)

    def test_unquoted_gradesheet_comma_is_a_row_error(self):
        text = (
            "Assignment,Quiz 1\n"
            "Date,2026-08-20\n"
            "\n"
            "Student Name,HW,D1\n"
            "Smith, Cali,,\n"
        )
        payload, err = parse_blank_gradesheet_csv_text(text, ["D1"])
        self.assertIsNone(payload)
        self.assertIn("put the student name in quotes", err)

    def test_padded_trailing_empty_columns_keep_a_quoted_comma_name(self):
        text = (
            "Assignment,Quiz 1\n"
            "Date,2026-08-20\n"
            "\n"
            "Student Name,HW,D1,,\n"
            '"Smith, Cali",,M,,\n'
        )
        payload, err = parse_blank_gradesheet_csv_text(text, ["D1"])
        self.assertIsNone(err)
        self.assertEqual(payload["students"][0]["name"], "Smith, Cali")

    def test_email_fixture_parses_in_canvas_order(self):
        fixture = os.path.join(
            os.path.dirname(__file__),
            "fixtures",
            "students_last_first_with_emails.csv",
        )
        with open(fixture, encoding="utf-8") as handle:
            csv_text = handle.read()
        rows, warnings = parse_students_csv_text(csv_text)
        self.assertEqual(warnings, [])
        self.assertEqual([row["full_name"] for row in rows], list(CANVAS_ORDER))

    def test_overdue_revisions_sort_by_stored_name(self):
        from app.routes import _sort_overdue_revisions
        rows = [
            {"student_name": "Zenith, Bob"},
            {"student_name": "Adams, Zoe"},
        ]
        ordered = _sort_overdue_revisions(rows)
        self.assertEqual(
            [row["student_name"] for row in ordered],
            ["Adams, Zoe", "Zenith, Bob"],
        )


def _revision_row(student_id, name, due, assignment, code="", objective="", assignment_id=None, top_score="R", learning_objective_id=""):
    return {
        "student_id": student_id,
        "student_name": name,
        "revision_due": due,
        "assignment_name": assignment,
        "assignment_id": assignment if assignment_id is None else assignment_id,
        "lo_code": code,
        "lo_objective_name": objective,
        "top_score": top_score,
        "learning_objective_id": learning_objective_id,
    }


class TestRevisionReport(unittest.TestCase):
    def test_local_today_at_02_utc_is_the_previous_pacific_date(self):
        from datetime import datetime, timezone, date
        from app.local_time import local_today
        app = create_app()
        instant = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)
        with app.app_context():
            self.assertEqual(local_today(instant), date(2026, 10, 20))

    def test_invalid_app_timezone_raises_at_startup(self):
        from config import Config
        class BadTimezone(Config):
            APP_TIMEZONE = "Not/ARealZone"
        with self.assertRaises(RuntimeError) as caught:
            create_app(BadTimezone)
        self.assertIn("Not/ARealZone", str(caught.exception))

    def test_revision_candidates_honor_the_passed_date(self):
        from datetime import date
        today = date(2026, 1, 1)
        def grade(student_id, due, score):
            return {
                "student_id": student_id,
                "assignment_id": "a-" + student_id,
                "top_score": "R",
                "hw_score_at_entry": score,
                "assignments": {
                    "id": "a-" + student_id,
                    "name": "Quiz",
                    "revision_due": due,
                },
                "learning_objectives": {
                    "id": "lo",
                    "name": "Limits",
                    "vendor_code": "T1",
                },
            }
        grades = [
            grade("past", "2025-12-31", 80),
            grade("today", "2026-01-01", 80),
            grade("day7", "2026-01-08", 80),
            grade("day8", "2026-01-09", 80),
            grade("low", "2026-01-01", 74),
            grade("blank", "", 80),
            grade("missing", None, 80),
        ]
        query = MagicMock()
        query.select.return_value = query
        query.in_.return_value = query
        query.eq.return_value = query
        query.order.return_value = query
        query.execute.return_value = MagicMock(data=[])
        with unittest.mock.patch(
            "app.models.Course.get_all_lo_ids_for_class", return_value=["lo"]
        ), unittest.mock.patch(
            "app.models.fetch_all_rows", return_value=grades
        ), unittest.mock.patch("app.models.supabase_admin") as sa:
            sa.table.return_value = query
            rows = Grade.get_revision_candidates("c1", today)
        self.assertEqual(
            sorted(row["revision_due"] for row in rows),
            ["2025-12-31", "2026-01-01", "2026-01-08"],
        )
        self.assertEqual(rows[0]["lo_code"], "T1")
        self.assertEqual(rows[0]["lo_objective_name"], "Limits")
        self.assertEqual(rows[0]["assignment_id"], "a-past")
        self.assertEqual(rows[0]["learning_objective_id"], "lo")

    def test_report_groups_exact_days_and_one_card_per_student(self):
        from datetime import date
        from app.routes import build_revision_report
        today = date(2026, 10, 20)
        rows = [
            _revision_row("s1", "Zenith, Bob", "2026-10-19", "Quiz B", "T2", "Derivatives"),
            _revision_row("s1", "Zenith, Bob", "2026-10-19", "Quiz A", "T1", "Limits"),
            _revision_row("s2", "Adams, Zoe", "2026-10-19", "Quiz A", "T1", "Limits"),
            _revision_row("s2", "Adams, Zoe", "2026-10-20", "Quiz C", "T3", "Integrals"),
            _revision_row("s3", "Lee, Ana", "2026-10-24", "Quiz D", "T4", "Series"),
            _revision_row("s4", "Ng, Bo", "2026-10-23", "Quiz E", "T5", "Proofs"),
            _revision_row("s5", "Cho, Min", "2026-10-21", "Quiz F"),
        ]
        report = build_revision_report(rows, today)
        views = {view["key"]: view for view in report["views"]}
        self.assertEqual(
            [view["key"] for view in report["views"]],
            ["overdue", "today", "1", "2", "3", "4", "5", "6", "7"],
        )
        overdue = views["overdue"]
        self.assertEqual(overdue["student_count"], 2)
        self.assertEqual(overdue["revision_count"], 3)
        self.assertEqual(report["overdue_revision_count"], 3)
        self.assertEqual(
            [student["student_name"] for student in overdue["students"]],
            ["Adams, Zoe", "Zenith, Bob"],
        )
        zenith = overdue["students"][1]
        self.assertEqual(len(zenith["assignments"]), 2)
        self.assertEqual(
            [item["assignment_name"] for item in zenith["assignments"]],
            ["Quiz A", "Quiz B"],
        )
        self.assertEqual(views["today"]["student_count"], 1)
        self.assertEqual(views["today"]["revision_count"], 1)
        self.assertEqual(views["today"]["students"][0]["student_id"], "s2")
        self.assertEqual(views["1"]["label"], "Due in 1 day")
        self.assertEqual(views["1"]["student_count"], 1)
        self.assertEqual(views["3"]["label"], "Due in 3 days ★")
        self.assertEqual(views["3"]["student_count"], 1)
        self.assertEqual(views["3"]["students"][0]["assignments"][0]["revision_due"], "2026-10-23")
        self.assertEqual(views["4"]["student_count"], 1)
        self.assertEqual(views["4"]["students"][0]["assignments"][0]["revision_due"], "2026-10-24")
        self.assertEqual(views["4"]["students"][0]["student_id"], "s3")
        self.assertNotIn("s3", [student["student_id"] for student in views["3"]["students"]])
        self.assertEqual(views["5"]["revision_count"], 0)
        self.assertEqual(views["5"]["empty_message"], "No revisions due in 5 days")
        self.assertEqual(views["4"]["count_line"], "1 student, 1 revision")
        self.assertEqual(overdue["count_line"], "2 students, 3 revisions")
        self.assertEqual(views["today"]["empty_message"], "No revisions due today")

    def test_due_on_the_pacific_date_is_not_overdue_at_02_utc(self):
        from datetime import datetime, timezone
        from app.local_time import local_today
        from app.routes import build_revision_report
        app = create_app()
        instant = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)
        with app.app_context():
            today = local_today(instant)
        report = build_revision_report(
            [_revision_row("s1", "Lee, Ana", "2026-10-20", "Quiz", "T1", "Limits")],
            today,
        )
        views = {view["key"]: view for view in report["views"]}
        self.assertEqual(views["overdue"]["revision_count"], 0)
        self.assertEqual(views["today"]["revision_count"], 1)
        self.assertEqual(views["today"]["student_count"], 1)

    def test_empty_day_message_for_four_days(self):
        from datetime import date
        from app.routes import build_revision_report
        report = build_revision_report([], date(2026, 10, 20))
        views = {view["key"]: view for view in report["views"]}
        self.assertEqual(views["4"]["empty_message"], "No revisions due in 4 days")
        self.assertEqual(views["4"]["label"], "Due in 4 days")
        self.assertEqual(views["overdue"]["revision_count"], 0)

    def test_assignments_group_by_id_and_objectives_sort_by_code(self):
        from datetime import date
        from app.routes import build_revision_report
        today = date(2026, 10, 20)
        rows = [
            _revision_row("s1", "Lee, Ana", "2026-10-19", "Quiz", "E10", "Series", assignment_id="a-late", learning_objective_id="lo-10"),
            _revision_row("s1", "Lee, Ana", "2026-10-18", "Quiz", "E2", "Limits", assignment_id="a-early", top_score="RQ", learning_objective_id="lo-2"),
            _revision_row("s1", "Lee, Ana", "2026-10-18", "Quiz", "Test 1.2", "Test 1.2", assignment_id="a-early", learning_objective_id="lo-test"),
        ]
        report = build_revision_report(rows, today)
        overdue = {view["key"]: view for view in report["views"]}["overdue"]
        self.assertEqual(overdue["student_count"], 1)
        self.assertEqual(overdue["revision_count"], 3)
        student = overdue["students"][0]
        self.assertEqual(
            [item["assignment_id"] for item in student["assignments"]],
            ["a-early", "a-late"],
        )
        self.assertEqual(
            [item["assignment_name"] for item in student["assignments"]],
            ["Quiz", "Quiz"],
        )
        early = student["assignments"][0]
        self.assertEqual([obj["lo_code"] for obj in early["objectives"]], ["E2", "Test 1.2"])
        self.assertTrue(early["objectives"][0]["show_name"])
        self.assertFalse(early["objectives"][1]["show_name"])
        self.assertEqual(early["objectives"][0]["top_score"], "RQ")


class TestRevisionModalTemplate(unittest.TestCase):
    def _render(self, report):
        from flask import render_template, session
        app = create_app()
        with app.test_request_context("/"):
            session["role"] = "instructor"
            return render_template(
                "class_detail.html",
                class_id="c1",
                class_name="Test Upload",
                class_days="MW",
                class_section="F26",
                students=[],
                all_students=[],
                learning_objectives=[],
                assignments=[],
                revision_report=report,
                revision_printed_on="2026-10-20",
            )

    def test_dropdown_counts_star_and_empty_day(self):
        from datetime import date
        from app.routes import build_revision_report
        today = date(2026, 10, 20)
        populated = build_revision_report([
            _revision_row("s1", "Adams, Zoe", "2026-10-19", "Quiz A", "T1", "Limits"),
            _revision_row("s2", "Lee, Ana", "2026-10-24", "Quiz B", "T2", "Derivatives"),
            _revision_row("s3", "Ng, Bo", "2026-10-24", "Quiz C"),
        ], today)
        html = self._render(populated)
        self.assertIn("Overdue (1)", html)
        self.assertIn("Due today (0)", html)
        self.assertIn("Due in 1 day (0)", html)
        self.assertIn("Due in 3 days ★ (0)", html)
        self.assertIn("Due in 4 days (2)", html)
        self.assertIn("2 students, 2 revisions", html)
        empty = self._render(build_revision_report([], today))
        self.assertIn("No revisions due in 4 days", empty)
        self.assertIn("No revisions due today", empty)
        self.assertIn("No overdue revisions.", empty)

    def test_print_frame_and_no_inner_html(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "templates", "class_detail.html",
        )
        with open(path, encoding="utf-8") as handle:
            src = handle.read()
        modal = src.split('id="overdueRevisionsModal"', 1)[1].split(
            'id="studentManagementModal"', 1
        )[0]
        self.assertIn("@page { size: letter portrait; margin: 0; }", modal)
        self.assertIn("overflow: visible !important;", modal)
        self.assertIn("height: auto !important;", modal)
        self.assertIn("break-inside: avoid;", modal)
        self.assertIn("padding: 0 0.2in !important;", modal)
        self.assertIn("display: table-footer-group !important;", modal)
        self.assertIn("class_display_title(class_name, class_days, class_section)", modal)
        self.assertIn('<thead class="revision-print-spacer">', modal)
        self.assertIn('<tfoot class="revision-print-spacer">', modal)
        self.assertIn("revision-modal-chrome", modal)
        self.assertIn("revision-grade", modal)
        print_block = modal.split("@media print", 1)[1]
        self.assertIn("column-count: 2", print_block)
        self.assertIn("break-inside: avoid", print_block)
        self.assertNotIn("innerHTML", modal)
        script = src[src.find("function selectRevisionView"):src.find("function openOverdueRevisionsModal")]
        self.assertNotIn("innerHTML", script)
        self.assertIn("classList.toggle", script)


class TestNormalizeProfile(unittest.TestCase):
    """Tests for normalize_profile — handles Supabase join shape quirks."""

    def test_dict_profile_returned_as_is(self):
        enrollment = {'profiles': {'id': '1', 'full_name': 'Alice'}}
        self.assertEqual(normalize_profile(enrollment), {'id': '1', 'full_name': 'Alice'})

    def test_list_profile_returns_first_element(self):
        enrollment = {'profiles': [{'id': '1', 'full_name': 'Alice'}]}
        self.assertEqual(normalize_profile(enrollment), {'id': '1', 'full_name': 'Alice'})

    def test_empty_list_returns_empty_dict(self):
        enrollment = {'profiles': []}
        self.assertEqual(normalize_profile(enrollment), {})

    def test_none_profile_returns_empty_dict(self):
        enrollment = {'profiles': None}
        self.assertEqual(normalize_profile(enrollment), {})

    def test_missing_key_returns_empty_dict(self):
        self.assertEqual(normalize_profile({}), {})


# ==========================================================================
# Constants Tests
# ==========================================================================

class TestConstants(unittest.TestCase):
    """Verify the shared grading constants are correct."""

    def test_mastery_grades_contains_expected_codes(self):
        self.assertIn('M', MASTERY_GRADES)
        self.assertIn('MR', MASTERY_GRADES)
        self.assertIn('R', MASTERY_GRADES)
        self.assertIn('RQ', MASTERY_GRADES)
        self.assertIn('P', MASTERY_GRADES)
        self.assertIn('X', MASTERY_GRADES)
        self.assertIn('A', MASTERY_GRADES)

    def test_default_required_ms(self):
        self.assertEqual(DEFAULT_REQUIRED_MS, 2)


# ==========================================================================
# Flask App Factory Tests
# ==========================================================================

class TestAppFactory(unittest.TestCase):
    """Tests for the create_app factory and basic configuration."""

    def setUp(self):
        self.app = create_app()
        self.app.config['TESTING'] = True

    def test_app_is_created(self):
        self.assertIsNotNone(self.app)

    def test_testing_flag(self):
        self.assertTrue(self.app.config['TESTING'])

    def test_blueprint_registered(self):
        self.assertIn('main', self.app.blueprints)

    def test_secret_key_is_set(self):
        self.assertIsNotNone(self.app.secret_key)

    def test_cors_configured(self):
        # CORS extension adds after_request handlers
        self.assertTrue(len(self.app.after_request_funcs) > 0)


# ==========================================================================
# Route Smoke Tests
# ==========================================================================

class TestRequestCacheMemoization(unittest.TestCase):
    """Repeated helper calls should hit DB once per request (memoized via flask.g).

    The assertions check call count, not just the result. The whole point of
    request-scope memoization is "exactly one DB round-trip per request per
    key", so a regression where the helper started returning the right value
    but bypassed the cache would still be a meaningful failure here.
    """

    def setUp(self):
        self.app = create_app()
        self.app.config['TESTING'] = True

    def _patch_supabase_admin(self, table_to_payload):
        """Return an unmagic-mock-style stub with .table().select().eq()....execute() chains."""
        from unittest.mock import MagicMock

        def make_query(payload):
            q = MagicMock()
            q.select.return_value = q
            q.eq.return_value = q
            q.in_.return_value = q
            q.is_.return_value = q
            q.ilike.return_value = q
            q.order.return_value = q
            q.limit.return_value = q
            q.single.return_value = q
            exec_mock = MagicMock()
            exec_mock.data = payload
            q.execute.return_value = exec_mock
            return q

        sa = MagicMock()
        call_count = {"total": 0}

        def table_side(name):
            call_count["total"] += 1
            return make_query(table_to_payload.get(name, []))

        sa.table.side_effect = table_side
        return sa, call_count

    def test_class_instructor_id_memoized(self):
        from app import routes as r
        sa, calls = self._patch_supabase_admin({
            "classes": [{"instructor_id": "u1"}],
        })
        with self.app.test_request_context("/"):
            with unittest.mock.patch.object(r, "supabase_admin", sa):
                a = r._class_instructor_id("c1")
                b = r._class_instructor_id("c1")
                self.assertEqual(a, "u1")
                self.assertEqual(b, "u1")
                self.assertEqual(calls["total"], 1)

    def test_class_instructor_id_non_string_is_not_owner(self):
        from app import routes as r
        from flask import session

        for raw in (7, None):
            sa, _calls = self._patch_supabase_admin({
                "classes": [{"instructor_id": raw}],
            })
            with self.app.test_request_context("/"):
                with unittest.mock.patch.object(r, "supabase_admin", sa):
                    session["user_id"] = "u1"
                    self.assertIsNone(r._class_instructor_id("c1"))
                    self.assertFalse(r._instructor_owns_class("c1"))

    def test_assignment_belongs_to_class_memoized(self):
        from app import routes as r
        sa, calls = self._patch_supabase_admin({
            "assignments": [{"id": "a1"}],
        })
        with self.app.test_request_context("/"):
            with unittest.mock.patch.object(r, "supabase_admin", sa):
                self.assertTrue(r._assignment_belongs_to_class("c1", "a1"))
                self.assertTrue(r._assignment_belongs_to_class("c1", "a1"))
                self.assertEqual(calls["total"], 1)


class TestPublicRoutes(unittest.TestCase):
    """Verify public pages are reachable without authentication."""

    def setUp(self):
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def test_login_page_returns_200(self):
        response = self.client.get('/login')
        self.assertEqual(response.status_code, 200)

    def test_root_returns_login(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)

    def test_signup_page_returns_200(self):
        response = self.client.get('/signup')
        self.assertEqual(response.status_code, 200)

    def test_logout_redirects(self):
        response = self.client.get('/logout')
        self.assertEqual(response.status_code, 302)


class TestRequireJsonObject(unittest.TestCase):
    """Strict JSON parsing helper used by state-changing API routes.

    Both 400 and 415 are exercised because a lenient client (sending the wrong
    Content-Type) and a buggy client (sending malformed JSON) need to be
    distinguishable: 415 tells the caller to fix headers, 400 tells them to
    fix payload shape. Conflating them used to mask real client bugs.
    """

    def setUp(self):
        self.app = create_app()
        self.app.config['TESTING'] = True

    def test_invalid_json_returns_400(self):
        from app.routes import _require_json_object

        with self.app.test_request_context(
            "/dummy",
            method="POST",
            data="{not json",
            content_type="application/json",
        ):
            data, err = _require_json_object()
            self.assertIsNone(data)
            self.assertIsNotNone(err)
            self.assertEqual(err[1], 400)

    def test_non_json_content_type_returns_415(self):
        from app.routes import _require_json_object

        with self.app.test_request_context(
            "/dummy",
            method="POST",
            data='{"a": 1}',
            content_type="text/plain",
        ):
            data, err = _require_json_object()
            self.assertIsNone(data)
            self.assertEqual(err[1], 415)

    def test_array_json_returns_400(self):
        from app.routes import _require_json_object

        with self.app.test_request_context(
            "/dummy",
            method="POST",
            data="[]",
            content_type="application/json",
        ):
            data, err = _require_json_object()
            self.assertIsNone(data)
            self.assertEqual(err[1], 400)

    def test_valid_object_returns_data(self):
        from app.routes import _require_json_object

        with self.app.test_request_context(
            "/dummy",
            method="POST",
            data='{"rows": []}',
            content_type="application/json",
        ):
            data, err = _require_json_object()
            self.assertIsNone(err)
            self.assertEqual(data, {"rows": []})


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
        from app import routes as r
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
                unittest.mock.patch.object(r.Course, "get_assignment_lo_ids", return_value=["lo-x"]), \
                unittest.mock.patch.object(
                    r.Homework,
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
        from app import routes as r
        with unittest.mock.patch.object(r.Homework, "decide_hw_score_update") as decide:
            rv_low, rows_low = self._patched_save_grades_call(hw_map={"stu-1": 64})
            rv_high, rows_high = self._patched_save_grades_call(hw_map={"stu-1": 80})
        decide.assert_not_called()
        self.assertEqual(rv_low.status_code, 200)
        self.assertEqual(rows_low[0]["counts_for_mastery"], False)
        self.assertEqual(rv_high.status_code, 200)
        self.assertEqual(rows_high[0]["counts_for_mastery"], True)


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
        from app import routes as r
        headers = self._session()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(
                    r.Homework, "canonicalize_homework_group_for_class", return_value=None
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
        from app import routes as r
        headers = self._session()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_assignment_belongs_to_class", return_value=True), \
                unittest.mock.patch.object(
                    r.Homework, "canonicalize_homework_group_for_class", return_value=None
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


class TestHwPassPromotesNonCountingMasteries(unittest.TestCase):
    """save_hw_percentage with score=-1 flips non-counting M/MR to counting."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_hw_pass_promotes_non_counting_masteries(self):
        from app import routes as r

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
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(
                    r, "_student_enrolled_in_class", return_value=True
                ), \
                unittest.mock.patch.object(
                    r.Homework,
                    "resolve_hw_group_storage_key",
                    return_value="hw-g1",
                ), \
                unittest.mock.patch.object(
                    r.Course,
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
        from app import routes as r

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
                    r.Homework,
                    "resolve_hw_group_storage_key",
                    return_value="hw-g1",
                ), \
                unittest.mock.patch.object(r.Homework, "decide_hw_score_update") as decide:
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

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def execute(self):
        result = MagicMock()
        result.data = list(self._rows)
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
        "new_assignment_objectives": new_aos,
        "old_assignment_objectives": old_aos,
    }


def _assignment_scoped_grade_letter(grades_map, student_id, lo_id):
    """Same lookup as assignmentScopedGradeLetter in class_reports.html."""
    key = f"{student_id}|{lo_id}"
    return str((grades_map or {}).get(key) or "").upper()


def _report_rows_from_scoped_grades(assignment_objectives, grades_map, student_id):
    """Same assignment-scoped branch as getIndividualReportParts in class_reports.html."""
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
    """Same body shape as buildEmailPayloadForStudent in class_reports.html."""
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
    lines.append("")
    lines.append("Sent by Your instructor via Clarity Grader")
    lines.append(f"Class: {class_name}")
    lines.append(f"You are receiving this because you are enrolled in {class_name}.")
    lines.append(
        "This is an automated message. Replies are not monitored. "
        "Please contact your instructor directly."
    )
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
        from app import routes as r
        sa = _filter_client(self.store)
        return (
            unittest.mock.patch.object(r, "supabase_admin", sa),
            unittest.mock.patch.object(r, "_instructor_owns_class", return_value=owns_class),
            unittest.mock.patch.object(r, "_assignment_belongs_to_class", return_value=belongs),
            unittest.mock.patch.object(
                r.Homework, "get_hw_scores_map_for_assignment", return_value={}
            ),
            unittest.mock.patch.object(
                r.Homework, "get_hw_prev_scores_map_for_assignment", return_value={}
            ),
        )

    def _get_assignment_grades(self, assignment_id, *, owns_class=True, belongs=True):
        patches = self._grades_patches(owns_class=owns_class, belongs=belongs)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            return self.client.get(
                f"/api/class/{CLASS_REPORTS_ID}/assignment/{assignment_id}/grades"
            )

    def _post_single_email(self, payload, *, owns_class=True, send_result=(True, "")):
        from app import routes as r
        sa = _filter_client(self.store)
        send_mock = unittest.mock.MagicMock(return_value=send_result)
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=owns_class), \
                unittest.mock.patch.object(r, "_send_via_resend", send_mock):
            rv = self.client.post(
                f"/api/class/{CLASS_REPORTS_ID}/student/{STU_ALEX_ID}/send-report-email",
                json=payload,
                headers=self._headers(),
            )
        return rv, send_mock

    def _post_bulk_email(self, payload, *, owns_class=True, send_result=(True, "")):
        from app import routes as r
        sa = _filter_client(self.store)
        send_mock = unittest.mock.MagicMock(return_value=send_result)
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
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
        send_mock.assert_called_once_with(
            STU_ALEX_EMAIL, subject, body, sender_name="Your instructor"
        )
        sent_body = send_mock.call_args[0][2]
        self.assertIn("D1  |  Not graded", sent_body)
        self.assertNotIn("D1  |  M", sent_body)

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
        send_mock.assert_called_once_with(
            STU_ALEX_EMAIL, subject, body, sender_name="Your instructor"
        )
        sent_body = send_mock.call_args[0][2]
        self.assertIn("Not graded", sent_body)
        self.assertNotIn("D1  |  M", sent_body)
        self.assertEqual(payload.get("failed"), 0)
        self.assertEqual(payload.get("skipped"), 0)

    def test_reports_template_print_email_uses_scoped_cache(self):
        template_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "app",
            "templates",
            "class_reports.html",
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
            os.path.dirname(__file__), "..", "app", "templates", "class_reports.html",
        )
        history_path = os.path.join(
            os.path.dirname(__file__), "..", "app", "templates", "student_history.html",
        )
        helpers_path = os.path.join(
            os.path.dirname(__file__), "..", "app", "static", "js", "ui_helpers.js",
        )
        with open(reports_path, encoding="utf-8") as fh:
            reports_src = fh.read()
        with open(history_path, encoding="utf-8") as fh:
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
            self.assertIn("reportEmailFooter", body)
        combined = reports_src + history_src + helpers_src
        self.assertNotIn("instructorLastName", combined)
        self.assertNotIn("instructorSignoff", combined)
        self.assertNotIn("toTitleCaseWord", combined)
        self.assertNotIn("Hello student,", combined)
        self.assertNotIn("via Project Clarity", combined)
        self.assertNotIn("Email send complete.", reports_src)
        self.assertIn("Replies are not monitored", helpers_src)
        self.assertIn("Sent ", reports_src)
        self.assertIn("Skipped ", reports_src)
        self.assertIn("Failed ", reports_src)


class TestInstructorTitleAndReportEmail(unittest.TestCase):
    FROM_EMAIL = "reports@claritygrader.net"

    def test_instructor_title(self):
        from app import routes as r
        self.assertEqual(r.instructor_title("Estes, Brody"), "Professor Estes")
        self.assertEqual(r.instructor_title("Brody Estes"), "Professor Estes")
        self.assertEqual(r.instructor_title(""), "Your instructor")
        self.assertEqual(r.instructor_title(None), "Your instructor")
        self.assertEqual(r.instructor_title("  Estes ,   Brody  "), "Professor Estes")
        self.assertEqual(r.instructor_title("  Brody    Estes  "), "Professor Estes")
        self.assertEqual(r.instructor_title("brody estes"), "Professor Estes")
        self.assertEqual(r.instructor_title("BRODY ESTES"), "Professor ESTES")

    def test_from_header_is_always_quoted(self):
        from app import routes as r
        self.assertEqual(
            r._reports_from_header("Professor Estes", self.FROM_EMAIL),
            '"Professor Estes via Clarity Grader" <reports@claritygrader.net>',
        )
        self.assertEqual(
            r._reports_from_header("Your instructor", self.FROM_EMAIL),
            '"Your instructor via Clarity Grader" <reports@claritygrader.net>',
        )
        self.assertEqual(
            r._reports_from_header("", self.FROM_EMAIL),
            '"Your instructor via Clarity Grader" <reports@claritygrader.net>',
        )
        self.assertEqual(
            r._reports_from_header("Professor St. Estes", self.FROM_EMAIL),
            '"Professor St. Estes via Clarity Grader" <reports@claritygrader.net>',
        )
        self.assertEqual(
            r._reports_from_header("Professor Estes, Jr.", self.FROM_EMAIL),
            '"Professor Estes, Jr. via Clarity Grader" <reports@claritygrader.net>',
        )

    def _send(self, post_mock, *, sender_name="Professor Estes", body="Hello Jane,"):
        from app import routes as r
        with unittest.mock.patch.dict(
            os.environ,
            {
                "RESEND_API_KEY": "test-key",
                "REPORTS_FROM_EMAIL": self.FROM_EMAIL,
            },
        ), unittest.mock.patch.object(r.requests, "post", post_mock):
            return r._send_via_resend(
                "student@example.edu",
                "Your progress report",
                body,
                sender_name=sender_name,
            )

    def _response(self, status, payload, text=""):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = payload
        resp.text = text
        return resp

    def test_send_success_requires_id_and_omits_reply_to(self):
        post = MagicMock(return_value=self._response(200, {"id": "re_123"}))
        with self.assertLogs("app.routes", level="INFO") as logs:
            ok, err = self._send(post)
        self.assertTrue(ok)
        self.assertEqual(err, "")
        sent = post.call_args.kwargs["json"]
        self.assertIn("html", sent)
        self.assertIn("text", sent)
        self.assertEqual(sent["text"], "Hello Jane,")
        self.assertIn("Hello Jane,", sent["html"])
        self.assertNotIn("white-space", sent["html"])
        self.assertNotIn("pre-wrap", sent["html"])
        self.assertNotIn("reply_to", sent)
        self.assertEqual(
            sent["from"],
            '"Professor Estes via Clarity Grader" <reports@claritygrader.net>',
        )
        logged = "\n".join(logs.output)
        self.assertIn("re_123", logged)
        self.assertIn("example.edu", logged)
        self.assertNotIn("student@", logged)

    def test_send_success_without_id_fails(self):
        post = MagicMock(return_value=self._response(200, {}))
        ok, err = self._send(post)
        self.assertFalse(ok)
        self.assertEqual(err, "Email provider rejected request.")

    def test_send_422_fails(self):
        post = MagicMock(return_value=self._response(422, {"message": "bad"}, text="bad"))
        ok, err = self._send(post)
        self.assertFalse(ok)
        self.assertEqual(err, "Email provider rejected request.")

    def test_send_timeout_fails(self):
        from app import routes as r
        post = MagicMock(side_effect=r.requests.Timeout())
        ok, err = self._send(post)
        self.assertFalse(ok)
        self.assertEqual(err, "Email send failed.")

    def test_send_missing_key_does_not_post(self):
        from app import routes as r
        post = MagicMock()
        with unittest.mock.patch.dict(os.environ, {"REPORTS_FROM_EMAIL": self.FROM_EMAIL}):
            os.environ.pop("RESEND_API_KEY", None)
            with unittest.mock.patch.object(r.requests, "post", post):
                ok, err = r._send_via_resend(
                    "student@example.edu",
                    "Subject",
                    "Hello Jane,",
                    sender_name="Professor Estes",
                )
        self.assertFalse(ok)
        self.assertEqual(err, "Email is not configured.")
        post.assert_not_called()

    def test_send_missing_from_address_does_not_post(self):
        from app import routes as r
        post = MagicMock()
        with unittest.mock.patch.dict(os.environ, {"RESEND_API_KEY": "test-key"}):
            os.environ.pop("REPORTS_FROM_EMAIL", None)
            with unittest.mock.patch.object(r.requests, "post", post):
                ok, err = r._send_via_resend(
                    "student@example.edu",
                    "Subject",
                    "Hello Jane,",
                    sender_name="Professor Estes",
                )
        self.assertFalse(ok)
        self.assertEqual(err, "Email is not configured.")
        post.assert_not_called()

    def test_html_escapes_student_name(self):
        body = "Hello <Jane> & Co,"
        post = MagicMock(return_value=self._response(200, {"id": "re_456"}))
        with self.assertLogs("app.routes", level="INFO"):
            ok, err = self._send(post, body=body)
        self.assertTrue(ok)
        self.assertEqual(err, "")
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["text"], body)
        self.assertIn("Hello &lt;Jane&gt; &amp; Co,", sent["html"])
        self.assertNotIn("<Jane>", sent["html"])

    def test_report_body_html_paragraphs_breaks_and_footer(self):
        from app import routes as r
        body = (
            "Hello <Jane> & Co,\n"
            "Class: Algebra\n"
            "\n"
            "D1  |  M\n"
            "D2  |  R\n"
            "\n"
            "Sent by Professor Estes via Clarity Grader\n"
            "Class: Algebra\n"
            "You are receiving this because you are enrolled in Algebra.\n"
            "This is an automated message. Replies are not monitored. "
            "Please contact your instructor directly."
        )
        html = r._report_body_html(body, "Progress <report> & more")
        paragraphs = html.split("<p ")[1:]
        self.assertEqual(len(paragraphs), 3)
        self.assertIn("Hello &lt;Jane&gt; &amp; Co,<br>Class: Algebra", paragraphs[0])
        self.assertIn("D1  |  M<br>D2  |  R", paragraphs[1])
        self.assertIn("font-size:14px", paragraphs[0])
        self.assertIn("color:#222222", paragraphs[0])
        self.assertNotIn("color:#666666", paragraphs[0])
        self.assertNotIn("color:#666666", paragraphs[1])
        self.assertIn("font-size:12px", paragraphs[2])
        self.assertIn("color:#666666", paragraphs[2])
        self.assertIn("border-top:1px solid #dddddd", paragraphs[2])
        self.assertIn("padding-top:12px", paragraphs[2])
        self.assertIn("Sent by Professor Estes via Clarity Grader<br>", paragraphs[2])
        self.assertNotIn("white-space", html)
        self.assertNotIn("pre-wrap", html)
        self.assertNotIn("<Jane>", html)
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn('<html lang="en">', html)
        self.assertIn('<meta charset="utf-8">', html)
        self.assertIn("<title>Progress &lt;report&gt; &amp; more</title>", html)
        self.assertIn('style="max-width:600px;"', html)


class TestParseBlankGradesheetCsv(unittest.TestCase):
    VENDORS = ["D1", "D2", "D3"]

    def test_parses_export_shape_keeps_blank_lo_keys(self):
        text = (
            "\ufeffAssignment,Quiz 1\n"
            "Date,2026-08-20\n"
            "\n"
            "Student Name,HW,D1,D2,D3\n"
            "Doe Jane,80,M,,R\n"
            "Smith Alex,,,\n"
        )
        payload, err = parse_blank_gradesheet_csv_text(text, self.VENDORS)
        self.assertIsNone(err)
        self.assertEqual(payload["assignment_name"], "Quiz 1")
        self.assertEqual(payload["learning_objectives"], ["D1", "D2", "D3"])
        self.assertEqual(payload["extraction_path"], "csv")
        by_name = {s["name"]: s for s in payload["students"]}
        self.assertEqual(by_name["Doe Jane"]["grades"], {"D1": "M", "D2": "", "D3": "R"})
        self.assertEqual(by_name["Doe Jane"]["homework_pct"], "80")
        self.assertEqual(by_name["Smith Alex"]["grades"], {"D1": "", "D2": "", "D3": ""})
        self.assertIsNone(by_name["Smith Alex"]["homework_pct"])

    def test_rejects_wrong_header_shape(self):
        payload, err = parse_blank_gradesheet_csv_text("Name,Score\nAda,M\n", self.VENDORS)
        self.assertIsNone(payload)
        self.assertIn("not a Clarity gradesheet export", err)

    def test_rejects_unknown_lo_header(self):
        text = (
            "Assignment,Quiz 1\n"
            "Date,2026-08-20\n"
            "\n"
            "Student Name,HW,D1,NOTALO\n"
            "Doe Jane,,M,P\n"
        )
        payload, err = parse_blank_gradesheet_csv_text(text, self.VENDORS)
        self.assertIsNone(payload)
        self.assertIn("Unknown", err)
        self.assertIn("NOTALO", err)

    def test_formula_prefix_and_quoted_name(self):
        text = (
            "Assignment,'=Quiz\n"
            "Date,2026-08-20\n"
            "\n"
            "Student Name,HW,D1\n"
            '"Smith, Alex",,M\n'
        )
        payload, err = parse_blank_gradesheet_csv_text(text, ["D1"])
        self.assertIsNone(err)
        self.assertEqual(payload["assignment_name"], "=Quiz")
        self.assertEqual(payload["students"][0]["name"], "Smith, Alex")
        self.assertEqual(payload["students"][0]["grades"], {"D1": "M"})

    def test_parses_three_metadata_rows_then_blank_then_header(self):
        text = (
            "Assignment,Quiz 1\n"
            "Date,2026-08-20\n"
            "NOTE,This is a blank template. Grade columns are intentionally empty. "
            "Re-uploading this file will CLEAR any grades already entered for this assignment.\n"
            "\n"
            "Student Name,HW,D1,D2,D3\n"
            "Doe Jane,80,M,,R\n"
            "Smith Alex,,,\n"
        )
        payload, err = parse_blank_gradesheet_csv_text(text, self.VENDORS)
        self.assertIsNone(err)
        self.assertEqual(payload["assignment_name"], "Quiz 1")
        self.assertEqual(payload["date_value"], "2026-08-20")
        self.assertEqual(payload["learning_objectives"], ["D1", "D2", "D3"])
        by_name = {s["name"]: s for s in payload["students"]}
        self.assertEqual(by_name["Doe Jane"]["grades"], {"D1": "M", "D2": "", "D3": "R"})
        self.assertEqual(by_name["Doe Jane"]["homework_pct"], "80")
        self.assertEqual(by_name["Smith Alex"]["grades"], {"D1": "", "D2": "", "D3": ""})
        self.assertIsNone(by_name["Smith Alex"]["homework_pct"])
        self.assertFalse(any(
            "NOTE" in (s.get("name") or "").upper() for s in payload["students"]
        ))

    def test_grades_follow_header_code_not_column_order(self):
        vendors = ["AO1", "AO2", "AO10", "CO1"]
        unsorted = (
            "Assignment,Quiz 1\n"
            "Date,2026-08-20\n"
            "\n"
            "Student Name,HW,AO10,AO2,CO1,AO1\n"
            "Doe Jane,80,R,M,P,X\n"
        )
        sorted_headers = (
            "Assignment,Quiz 1\n"
            "Date,2026-08-20\n"
            "\n"
            "Student Name,HW,AO1,AO2,AO10,CO1\n"
            "Doe Jane,80,X,M,R,P\n"
        )
        old, err_old = parse_blank_gradesheet_csv_text(unsorted, vendors)
        new, err_new = parse_blank_gradesheet_csv_text(sorted_headers, vendors)
        self.assertIsNone(err_old)
        self.assertIsNone(err_new)
        self.assertEqual(old["learning_objectives"], ["AO10", "AO2", "CO1", "AO1"])
        self.assertEqual(new["learning_objectives"], ["AO1", "AO2", "AO10", "CO1"])
        self.assertEqual(old["students"][0]["grades"], new["students"][0]["grades"])
        self.assertEqual(
            old["students"][0]["grades"],
            {"AO10": "R", "AO2": "M", "CO1": "P", "AO1": "X"},
        )


class TestGradesheetExportPrefill(unittest.TestCase):
    def test_csv_format_hw_score(self):
        self.assertEqual(_csv_format_hw_score(None), "")
        self.assertEqual(_csv_format_hw_score(85), "85")
        self.assertEqual(_csv_format_hw_score(85.0), "85")
        self.assertEqual(_csv_format_hw_score(-1), "-1")

    def test_data_rows_prefill_hw_and_letters_leave_true_gaps_blank(self):
        students = [
            {"id": "stu-1", "full_name": "Doe, Jane"},
            {"id": "stu-2", "full_name": "Smith, Alex"},
        ]
        los = [
            {"id": "lo-d1", "vendor_code": "D1"},
            {"id": "lo-d2", "vendor_code": "D2"},
        ]
        hw_map = {"stu-1": 85}
        letter_map = {("stu-1", "lo-d1"): "M"}
        rows = _gradesheet_csv_data_rows(students, los, hw_map, letter_map)
        self.assertEqual(rows[0], ["Doe, Jane", "85", "M", ""])
        self.assertEqual(rows[1], ["Smith, Alex", "", "", ""])

    def test_letter_map_from_assignment_rows(self):
        mapped = _gradesheet_letter_map_from_rows([
            {"student_id": "stu-1", "learning_objective_id": "lo-d1", "top_score": "M"},
            {"student_id": "stu-1", "learning_objective_id": "lo-d2", "top_score": None},
        ])
        self.assertEqual(mapped[("stu-1", "lo-d1")], "M")
        self.assertNotIn(("stu-1", "lo-d2"), mapped)

    def test_letter_map_keeps_every_grade_past_the_row_cap(self):
        from app.routes import _gradesheet_letter_map_for_assignment

        rows = []
        students = []
        for student_index in range(101):
            sid = "s%03d" % student_index
            students.append({"id": sid})
            for lo_index in range(11):
                rows.append({
                    "id": "%s-lo%02d" % (sid, lo_index),
                    "student_id": sid,
                    "learning_objective_id": "lo%02d" % lo_index,
                    "top_score": "M",
                })
        los = [{"id": "lo%02d" % lo_index} for lo_index in range(11)]
        pages = []

        class _Grades:
            def __init__(self):
                self.batch = []
                self.start = None
                self.end = None

            def select(self, *args, **kwargs):
                return self

            def eq(self, *args, **kwargs):
                return self

            def in_(self, column, values):
                if column == "student_id":
                    self.batch = list(values)
                return self

            def order(self, *args, **kwargs):
                return self

            def range(self, start, end):
                self.start = start
                self.end = end
                return self

            def execute(self):
                matched = [row for row in rows if row["student_id"] in set(self.batch)]
                if self.start is None:
                    data = matched[:1000]
                else:
                    end = self.end
                    if end is None:
                        data = []
                    else:
                        data = matched[self.start:end + 1]
                        pages.append((len(self.batch), self.start, end, len(matched)))
                return unittest.mock.MagicMock(data=data)

        class _Admin:
            def table(self, name):
                self.last = name
                return _Grades()

        with unittest.mock.patch("app.routes.supabase_admin", _Admin()):
            letter_map = _gradesheet_letter_map_for_assignment("asg-1", students, los)

        self.assertEqual(len(letter_map), 101 * 11)
        batch_sizes = []
        seen = []
        for size, start, end, matched in pages:
            if not seen or seen[-1] != size:
                batch_sizes.append(size)
                seen.append(size)
        self.assertEqual(batch_sizes, [100, 1])
        self.assertIn((100, 0, 999, 1100), pages)
        self.assertIn((100, 1000, 1999, 1100), pages)
        self.assertEqual(letter_map[("s000", "lo00")], "M")
        self.assertEqual(letter_map[("s100", "lo10")], "M")

    def test_blank_template_csv_has_note_and_empty_grade_cells(self):
        students = [{"id": "stu-1", "full_name": "Doe, Jane"}]
        los = [{"id": "lo-d1", "vendor_code": "D1"}]
        hw_map = {"stu-1": 85}
        data_rows = _gradesheet_csv_data_rows(students, los, hw_map, {})
        text = _build_gradesheet_csv_text("Quiz 1", "2026-08-20", ["D1"], data_rows, True)
        self.assertIn(_BLANK_GRADESHEET_NOTE, text)
        payload, err = parse_blank_gradesheet_csv_text(text, ["D1"])
        self.assertIsNone(err)
        self.assertEqual(payload["students"][0]["homework_pct"], "85")
        self.assertEqual(payload["students"][0]["grades"], {"D1": ""})

    def test_filled_export_csv_has_letters_and_no_note(self):
        students = [{"id": "stu-1", "full_name": "Doe, Jane"}]
        los = [{"id": "lo-d1", "vendor_code": "D1"}]
        hw_map = {"stu-1": 85}
        letter_map = {("stu-1", "lo-d1"): "M"}
        data_rows = _gradesheet_csv_data_rows(students, los, hw_map, letter_map)
        text = _build_gradesheet_csv_text("Quiz 1", "2026-08-20", ["D1"], data_rows, False)
        self.assertNotIn(_BLANK_GRADESHEET_NOTE, text)
        payload, err = parse_blank_gradesheet_csv_text(text, ["D1"])
        self.assertIsNone(err)
        self.assertEqual(payload["students"][0]["homework_pct"], "85")
        self.assertEqual(payload["students"][0]["grades"], {"D1": "M"})

    def test_filled_helper_puts_hw_prev_after_hw_and_blank_omits_it(self):
        import csv
        students = [
            {"id": "stu-1", "full_name": "Doe, Jane"},
            {"id": "stu-2", "full_name": "Smith, Alex"},
        ]
        los = [{"id": "lo-d1", "vendor_code": "D1"}]
        hw_map = {"stu-1": 85, "stu-2": 0}
        prev_map = {"stu-1": None, "stu-2": -1}
        rows = _gradesheet_csv_data_rows(
            students, los, hw_map, {}, True, prev_map
        )
        self.assertEqual(rows[0], ["Doe, Jane", "", "85", ""])
        self.assertEqual(rows[1], ["Smith, Alex", "'-1", "0", ""])
        filled = _build_gradesheet_csv_text(
            "Quiz 1", "2026-08-20", ["D1"], rows, False, True
        )
        filled_rows = list(csv.reader(io.StringIO(filled.lstrip("\ufeff"))))
        filled_header = next(row for row in filled_rows if row and row[0] == "Student Name")
        self.assertEqual(filled_header[:3], ["Student Name", "HW prev", "HW"])
        blank_rows = _gradesheet_csv_data_rows(students, los, hw_map, {})
        blank = _build_gradesheet_csv_text(
            "Quiz 1", "2026-08-20", ["D1"], blank_rows, True, False
        )
        blank_parsed = list(csv.reader(io.StringIO(blank.lstrip("\ufeff"))))
        blank_header = next(row for row in blank_parsed if row and row[0] == "Student Name")
        self.assertEqual(blank_header, ["Student Name", "HW", "D1"])
        self.assertNotIn("HW prev", blank_header)

    def test_parser_ignores_hw_prev_cells_including_between_objectives(self):
        vendors = ["D1", "D2"]
        without = (
            "Assignment,Quiz 1\n"
            "Date,2026-08-20\n"
            "\n"
            "Student Name,HW,D1,D2\n"
            "Doe Jane,80,M,R\n"
        )
        after_hw = (
            "Assignment,Quiz 1\n"
            "Date,2026-08-20\n"
            "\n"
            "Student Name,HW,HW prev,D1,D2\n"
            "Doe Jane,80,1,M,R\n"
        )
        between = (
            "Assignment,Quiz 1\n"
            "Date,2026-08-20\n"
            "\n"
            "Student Name,HW,D1,HW prev,D2\n"
            "Doe Jane,80,M,99,R\n"
        )
        base, err = parse_blank_gradesheet_csv_text(without, vendors)
        self.assertIsNone(err)
        for text in (after_hw, between):
            payload, parse_err = parse_blank_gradesheet_csv_text(text, vendors)
            self.assertIsNone(parse_err)
            self.assertEqual(
                payload["students"][0]["homework_pct"],
                base["students"][0]["homework_pct"],
            )
            self.assertEqual(
                payload["students"][0]["grades"],
                base["students"][0]["grades"],
            )
            self.assertNotIn("HW prev", payload["learning_objectives"])

    def test_csv_columns_only_los_linked_to_assignment(self):
        from app import routes as r

        pool = [
            {"id": "lo-1", "vendor_code": "A1"},
            {"id": "lo-2", "vendor_code": "A2"},
            {"id": "lo-3", "vendor_code": "B1"},
            {"id": "lo-4", "vendor_code": "B2"},
            {"id": "lo-5", "vendor_code": "C1"},
        ]
        q = MagicMock()
        q.select.return_value = q
        q.eq.return_value = q
        q.limit.return_value = q
        q.execute.return_value = MagicMock(
            data=[{"id": "asg-1", "name": "Quiz 1", "date_returned": "2026-08-20"}]
        )
        with unittest.mock.patch.object(r, "supabase_admin") as sa, \
                unittest.mock.patch.object(
                    r.Course, "get_full_class_data", return_value={"name": "C"}
                ), \
                unittest.mock.patch.object(
                    r,
                    "_process_enrollments",
                    return_value=([{"id": "stu-1", "full_name": "Doe, Jane"}], [], {}),
                ), \
                unittest.mock.patch.object(
                    r.Course, "get_learning_objectives", return_value=pool
                ), \
                unittest.mock.patch.object(
                    r.Course, "get_assignment_lo_ids", return_value=["lo-2", "lo-4"]
                ), \
                unittest.mock.patch.object(
                    r.Homework, "get_hw_scores_map_for_assignment", return_value={"stu-1": 80}
                ):
            sa.table.return_value = q
            bundle = r._gradesheet_export_bundle("class-1", "asg-1")

        assignment, students, learning_objectives, hw_map = bundle
        self.assertEqual(
            [lo["vendor_code"] for lo in learning_objectives],
            ["A2", "B2"],
        )
        letter_map = {("stu-1", "lo-2"): "M", ("stu-1", "lo-4"): "R"}
        vendor_codes = [lo["vendor_code"] for lo in learning_objectives]
        data_rows = _gradesheet_csv_data_rows(
            students, learning_objectives, hw_map, letter_map
        )
        text = _build_gradesheet_csv_text(
            "Quiz 1", "2026-08-20", vendor_codes, data_rows, False
        )
        payload, err = parse_blank_gradesheet_csv_text(
            text, ["A1", "A2", "B1", "B2", "C1"]
        )
        self.assertIsNone(err)
        self.assertEqual(payload["learning_objectives"], ["A2", "B2"])
        self.assertEqual(payload["students"][0]["grades"], {"A2": "M", "B2": "R"})
        self.assertNotIn("A1", payload["students"][0]["grades"])
        self.assertNotIn("C1", payload["students"][0]["grades"])

    def test_prev_before_hw_imports_the_hw_cell(self):
        text = (
            "Assignment,Quiz 1\n"
            "Date,2026-08-20\n"
            "\n"
            "Student Name,HW prev,HW,D1\n"
            "Doe Jane,1,80,M\n"
        )
        payload, err = parse_blank_gradesheet_csv_text(text, ["D1"])
        self.assertIsNone(err)
        self.assertEqual(payload["students"][0]["homework_pct"], "80")
        self.assertEqual(payload["students"][0]["grades"], {"D1": "M"})

    def test_hw_prev_after_objective_codes_imports_hw_and_marks(self):
        text = (
            "Assignment,Quiz 1\n"
            "Date,2026-08-20\n"
            "\n"
            "Student Name,HW,D1,D2,HW prev\n"
            "Doe Jane,80,M,R,99\n"
        )
        payload, err = parse_blank_gradesheet_csv_text(text, ["D1", "D2"])
        self.assertIsNone(err)
        self.assertEqual(payload["students"][0]["homework_pct"], "80")
        self.assertEqual(payload["students"][0]["grades"], {"D1": "M", "D2": "R"})

    def test_filled_export_parses_like_hw_only_file(self):
        students = [{"id": "stu-1", "full_name": "Doe Jane"}]
        los = [{"id": "lo-d1", "vendor_code": "D1"}]
        filled_rows = _gradesheet_csv_data_rows(
            students, los, {"stu-1": 85}, {("stu-1", "lo-d1"): "M"}, True, {"stu-1": 10}
        )
        filled = _build_gradesheet_csv_text(
            "Quiz 1", "2026-08-20", ["D1"], filled_rows, False, True
        )
        hw_only = (
            "Assignment,Quiz 1\n"
            "Date,2026-08-20\n"
            "\n"
            "Student Name,HW,D1\n"
            "Doe Jane,85,M\n"
        )
        filled_payload, filled_err = parse_blank_gradesheet_csv_text(filled, ["D1"])
        only_payload, only_err = parse_blank_gradesheet_csv_text(hw_only, ["D1"])
        self.assertIsNone(filled_err)
        self.assertIsNone(only_err)
        self.assertEqual(
            filled_payload["students"][0]["homework_pct"],
            only_payload["students"][0]["homework_pct"],
        )
        self.assertEqual(
            filled_payload["students"][0]["grades"],
            only_payload["students"][0]["grades"],
        )

    def test_old_hw_column_two_without_prev_is_unchanged(self):
        text = (
            "Assignment,Quiz 1\n"
            "Date,2026-08-20\n"
            "\n"
            "Student Name,HW,D1,D2\n"
            "Doe Jane,80,M,R\n"
        )
        payload, err = parse_blank_gradesheet_csv_text(text, ["D1", "D2"])
        self.assertIsNone(err)
        self.assertEqual(payload["learning_objectives"], ["D1", "D2"])
        self.assertEqual(payload["students"][0]["homework_pct"], "80")
        self.assertEqual(payload["students"][0]["grades"], {"D1": "M", "D2": "R"})

    def test_hw_prev_without_hw_header_returns_header_error(self):
        text = (
            "Assignment,Quiz 1\n"
            "Date,2026-08-20\n"
            "\n"
            "Student Name,HW prev,D1\n"
            "Doe Jane,80,M\n"
        )
        payload, err = parse_blank_gradesheet_csv_text(text, ["D1"])
        self.assertIsNone(payload)
        self.assertEqual(err, _GRADESHEET_HEADER_ERROR)

    def test_two_hw_headers_return_header_error(self):
        text = (
            "Assignment,Quiz 1\n"
            "Date,2026-08-20\n"
            "\n"
            "Student Name,HW,D1,HW\n"
            "Doe Jane,80,M,70\n"
        )
        payload, err = parse_blank_gradesheet_csv_text(text, ["D1"])
        self.assertIsNone(payload)
        self.assertEqual(err, _GRADESHEET_HEADER_ERROR)


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
        from app import routes as r
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
        with unittest.mock.patch.object(r, "_rate_limit", return_value=True), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_assignment_belongs_to_class", return_value=True), \
                unittest.mock.patch.object(
                    r.Homework, "resolve_hw_group_storage_key", return_value="quiz-1"
                ), \
                unittest.mock.patch.object(
                    r.Homework, "get_hw_scores_map_for_assignment", return_value=hw_map
                ), \
                unittest.mock.patch.object(r, "supabase_admin", sa):
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
        from app import routes as r

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
                unittest.mock.patch.object(r.Course, "get_full_class_data", return_value=class_data), \
                unittest.mock.patch.object(r, "load_assignments_for_class", return_value=[]), \
                unittest.mock.patch.object(r, "render_template", side_effect=fake_render):
            rv = client.get("/class/class-a/update_grade")
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(captured["template"], "update_grade.html")
        roster = captured["roster"]
        self.assertEqual(roster, [
            {"id": "stu-muted", "full_name": "Adams, Zoe"},
            {"id": "stu-active", "full_name": "Zenith, Bob"},
        ])
        for row in roster:
            self.assertEqual(set(row.keys()), {"id", "full_name"})

    def test_update_grade_post_is_not_allowed(self):
        from app import routes as r

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
            os.path.dirname(__file__), "..", "app", "templates", "update_grade.html",
        )
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn(
            '<script type="application/json" id="classRosterJson">{{ roster|tojson }}</script>',
            src,
        )
        self.assertIn("csv_wizard.js') }}?v=20", src)

    def test_speed_grader_autosave_runs_when_the_tab_hides(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "templates", "class_speed_grader.html",
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

    def test_csv_wizard_posts_ignored_names_without_preview(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "static", "js", "csv_wizard.js",
        )
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("preview-import-name-matches", src)
        self.assertNotIn("name_resolutions", src)
        self.assertIn("ignored_names", src)

    def test_csv_match_wizard_keeps_single_match_on_step_3(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "static", "js", "csv_wizard.js",
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
            os.path.join("templates", "update_grade.html"),
            os.path.join("static", "js", "csv_wizard.js"),
        )
        forbidden = ("mapModeCreate", "prefillCreateAssignmentFromCsv", "create_assignment")
        for name in names:
            path = os.path.join(os.path.dirname(__file__), "..", "app", name)
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            for token in forbidden:
                self.assertNotIn(token, src, name)

    def test_csv_wizard_replaces_pdf_analyzer_script(self):
        root = os.path.join(os.path.dirname(__file__), "..", "app")
        with open(os.path.join(root, "templates", "update_grade.html"), encoding="utf-8") as fh:
            template = fh.read()
        with open(os.path.join(root, "static", "js", "csv_wizard.js"), encoding="utf-8") as fh:
            script = fh.read()
        self.assertIn("js/csv_wizard.js') }}?v=20", template)
        self.assertFalse(os.path.exists(os.path.join(root, "static", "js", "pdf_analyzer.js")))
        for src in (template, script):
            self.assertNotIn("pdfInput", src)
            self.assertNotIn("uploadedPDFData", src)
            self.assertNotIn("analyzePDF", src)

    def test_add_student_flag_inputs_are_not_native_constraints(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "static", "js", "csv_wizard.js",
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
            os.path.dirname(__file__), "..", "app", "static", "js", "csv_wizard.js",
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
        from app import routes as r
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
                    r.Course, "get_learning_objectives", return_value=[{"vendor_code": "D1"}]
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
        from app import routes as r
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
                    r.Course, "get_learning_objectives", return_value=[{"vendor_code": "D1"}]
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
        from app import routes as r
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(
                    r.Course, "get_learning_objectives", return_value=[{"vendor_code": "D1"}]
                ):
            rv = self.client.post(
                "/api/class/c1/parse-grade-csv",
                data={"file": (io.BytesIO(b"nope"), "bad.csv")},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 400)
        self.assertIn("not a Clarity gradesheet export", rv.get_json().get("error") or "")

    def _post_grade_csv(self, csv_text, assignment_rows, owns=True):
        from app import routes as r
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
                    r.Course, "get_learning_objectives", return_value=[{"vendor_code": "D1"}]
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
        from app import routes as r
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
                    r.Course, "get_assignment_lo_ids", return_value=linked_lo_ids
                ), \
                unittest.mock.patch.object(
                    r.Homework, "get_hw_scores_map_for_assignment", return_value=hw_map
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
        from app import routes as r
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
                    r.Course, "get_lo_ids_for_class", return_value=["lo-any"]
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
        from app import routes as r
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["csrf_token"] = "test-csrf"

        with unittest.mock.patch.object(r, "_rate_limit", return_value=False):
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
        from app import routes as r
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

        with unittest.mock.patch.object(r, "_rate_limit", return_value=True), \
                unittest.mock.patch.object(r, "supabase_admin", sa):
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
            unittest.mock.patch.object(r.Course, "get_assignment_lo_ids", return_value=["lo-x"]),
            unittest.mock.patch.object(
                r.Homework, "get_hw_scores_map_for_assignment", return_value=hw_map
            ),
        )

    def test_save_grades_empty_payload_writes_no_audit_row(self):
        from app import routes as r
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
        from app import routes as r
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
        from app import routes as r
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
        from app import routes as r
        sa, grade_rows, log_rows = self._make_sa_mock(existing_grades=[{
            "student_id": "stu-1",
            "learning_objective_id": "lo-x",
            "assignment_id": "asg-1",
            "top_score": "M",
            "counts_for_mastery": False,
        }])
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r.Course, "get_all_lo_ids_for_class", return_value=["lo-x"]):
            n = r._promote_non_counting_masteries_for_student("class-1", "stu-1", changed_by="inst1")
        self.assertEqual(n, 1)
        self.assertEqual(grade_rows[0]["last_modified_by"], "inst1")
        self.assertEqual(log_rows, [])

    def test_allowed_operations_and_python_writer(self):
        from app import routes as r
        self.assertEqual(
            r.GRADE_LOG_OPERATIONS,
            (r.GRADE_LOG_INSERT, r.GRADE_LOG_UPDATE, r.GRADE_LOG_DELETE),
        )
        self.assertEqual(r.GRADE_LOG_OPERATIONS, ("INSERT", "UPDATE", "DELETE"))
        sa, _grade_rows, log_rows = self._make_sa_mock()
        with unittest.mock.patch.object(r, "supabase_admin", sa):
            r._log_grade_deletions(
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
        self.assertEqual(ops, {r.GRADE_LOG_DELETE})
        self.assertTrue(ops <= set(r.GRADE_LOG_OPERATIONS))

    def test_constraint_violation_logs_error_with_operation(self):
        from app import routes as r

        class CheckViolation(Exception):
            def __init__(self):
                super().__init__("grade_change_log_operation_check")
                self.code = "23514"

        q = MagicMock()
        q.insert.return_value = q
        q.execute.side_effect = CheckViolation()
        sa = MagicMock()
        sa.table.return_value = q
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r.logger, "error") as err, \
                unittest.mock.patch.object(r.logger, "warning") as warn:
            r._log_grade_deletions(
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
        self.assertIn(r.GRADE_LOG_DELETE, err.call_args[0])

    def test_missing_table_logs_warning_only(self):
        from app import routes as r

        class MissingTable(Exception):
            def __init__(self):
                super().__init__({"code": "42P01", "message": "grade_change_log does not exist"})

        q = MagicMock()
        q.insert.return_value = q
        q.execute.side_effect = MissingTable()
        sa = MagicMock()
        sa.table.return_value = q
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r.logger, "error") as err, \
                unittest.mock.patch.object(r.logger, "warning") as warn:
            r._log_grade_deletions(
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
        warn.assert_called()


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
        from app import routes as r
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
                    r.Homework, "canonicalize_homework_group_for_class", return_value="hw1"
                ), \
                unittest.mock.patch.object(r.Course, "get_lo_ids_for_class", return_value=[]):
            rv = self.client.post(
                "/class/c1/create_assignment",
                json={"name": "A" * 255, "homework_group": "hw1"},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertNotEqual(rv.status_code, 400)

    def test_create_assignment_name_256_rejected(self):
        """256 chars must be rejected with 400 and no DB call."""
        from app import routes as r
        sa = MagicMock()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(
                    r.Homework, "canonicalize_homework_group_for_class", return_value="hw1"
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
        from app import routes as r
        sa = MagicMock()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(
                    r.Homework, "canonicalize_homework_group_for_class",
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
        from app import routes as r
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
        from app import routes as r
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
        self.assertEqual(set(profile_row), {"id", "full_name", "role", "email"})

    def test_add_student_rejects_a_256_character_comma_name_before_the_database(self):
        from app import routes as r
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
        from app import routes as r
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
        from app import routes as r
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
        self.assertEqual(set(payload), {"full_name", "email"})

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
        from app import routes as r
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
        from app import routes as r
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
        from app import routes as r
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
        from app import routes as r
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
        from app import routes as r
        sa = self._report_email_sa_mock()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_rate_limit", return_value=True), \
                unittest.mock.patch.object(r, "_send_via_resend", return_value=(True, None)):
            rv = self.client.post(
                "/api/class/c1/student/s1/send-report-email",
                json={"subject": "S" * 255, "body": "hello"},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertNotEqual(rv.status_code, 400, "255-char subject must not be length-rejected")

    def test_report_email_subject_256_rejected(self):
        """256-char subject must be rejected with 400; _send_via_resend must not be called."""
        from app import routes as r
        send_mock = unittest.mock.MagicMock()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_rate_limit", return_value=True), \
                unittest.mock.patch.object(r, "_send_via_resend", send_mock):
            rv = self.client.post(
                "/api/class/c1/student/s1/send-report-email",
                json={"subject": "S" * 256, "body": "hello"},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 400)
        send_mock.assert_not_called()

    def test_report_email_body_10000_accepted(self):
        """Exactly 10 000-char body must pass the length check."""
        from app import routes as r
        sa = self._report_email_sa_mock()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_rate_limit", return_value=True), \
                unittest.mock.patch.object(r, "_send_via_resend", return_value=(True, None)):
            rv = self.client.post(
                "/api/class/c1/student/s1/send-report-email",
                json={"subject": "subject", "body": "b" * 10000},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertNotEqual(rv.status_code, 400, "10 000-char body must not be length-rejected")

    def test_report_email_body_10001_rejected(self):
        """10 001-char body must be rejected with 400; _send_via_resend must not be called."""
        from app import routes as r
        send_mock = unittest.mock.MagicMock()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_rate_limit", return_value=True), \
                unittest.mock.patch.object(r, "_send_via_resend", send_mock):
            rv = self.client.post(
                "/api/class/c1/student/s1/send-report-email",
                json={"subject": "subject", "body": "b" * 10001},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 400)
        send_mock.assert_not_called()



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
        from app import routes as r
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True):
            return client.post(
                "/class/c1/add_student",
                json=body,
                headers={"X-CSRF-Token": "test-csrf"},
            )

    def _post_edit(self, client, sa, body, student_id="stu-1"):
        from app import routes as r
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
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
        self.assertEqual(profiles.updates, [{
            "full_name": "Smith, Cali",
            "email": "shared@example.edu",
        }])
        self.assertEqual(classes.eq_calls, [("instructor_id", "inst1")])
        self.assertFalse(profiles.selected)

    def test_csv_wizard_pushes_the_stored_name(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "static", "js", "csv_wizard.js",
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
        from app import routes as r
        payload = {"file": (io.BytesIO(text.encode("utf-8")), "students.csv")}
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_rate_limit", return_value=True):
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
            os.path.dirname(__file__), "..", "app", "templates", "class_detail.html",
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
        from app.routes import decide_student_enrollment
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
        from app import routes as r
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
        from app import routes as r
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
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r.Course, "get_all_lo_ids_for_class", return_value=[]):
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


class _PagingResult:
    def __init__(self, data):
        self.data = data


class _PagingQuery:
    """One PostgREST builder. execute returns at most 1000 rows.

    range is applied only when order was set. Without order, every execute
    returns the first 1000 rows, which is the unordered cap.
    """

    def __init__(self, rows, sink):
        self._rows = list(rows)
        self._sink = sink
        self._eq = []
        self._in = []
        self._orders = []
        self._range = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, column, value):
        self._eq.append((column, value))
        return self

    def in_(self, column, values):
        self._in.append((column, {str(v) for v in values}))
        return self

    def order(self, column, desc=False, nullsfirst=False):
        self._orders.append(column)
        return self

    def limit(self, _n):
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def execute(self):
        rows = list(self._rows)
        for column, value in self._eq:
            rows = [row for row in rows if row.get(column) == value]
        for column, allowed in self._in:
            rows = [row for row in rows if str(row.get(column)) in allowed]
        if not self._orders:
            sliced = rows[:1000]
        else:
            rows = sorted(
                rows,
                key=lambda row: tuple((row.get(column) or "") for column in self._orders),
            )
            start, end = self._range if self._range is not None else (0, 999)
            self._sink.append((tuple(self._orders), start, end, len(rows)))
            sliced = rows[start:end + 1]
            if len(sliced) > 1000:
                sliced = sliced[:1000]
        return _PagingResult(sliced)


class _PagingClient:
    def __init__(self, tables):
        self.tables = tables
        self.grade_builds = 0
        self.grade_pages = []

    def table(self, name):
        sink = self.grade_pages if name == "grades" else []
        if name == "grades":
            self.grade_builds += 1
        return _PagingQuery(self.tables.get(name, []), sink)


class TestFullClassDataGradePaging(unittest.TestCase):
    def _client(self, with_objectives):
        students = [f"stu-{i:02d}" for i in range(38)]
        class_lo = "lo-class"
        other_lo = "lo-other"
        grades = []
        for sid in students:
            for n in range(40):
                grades.append({
                    "id": f"g-{sid}-{n}",
                    "student_id": sid,
                    "learning_objective_id": class_lo,
                    "assignment_id": f"asg-{n:02d}",
                    "top_score": "M",
                    "second_score": None,
                    "counts_for_mastery": True,
                    "learning_objectives": {
                        "id": class_lo,
                        "vendor_code": "D1",
                        "required_ms": 2,
                    },
                })
            grades.append({
                "id": f"g-{sid}-other",
                "student_id": sid,
                "learning_objective_id": other_lo,
                "assignment_id": "asg-other",
                "top_score": "M",
                "second_score": None,
                "counts_for_mastery": True,
                "learning_objectives": {
                    "id": other_lo,
                    "vendor_code": "D1",
                    "required_ms": 2,
                },
            })
        tables = {
            "classes": [{"id": "class-mw", "name": "MW"}],
            "learning_objectives": (
                [{"id": class_lo, "name": "D1", "vendor_code": "D1", "description": None, "required_ms": 2, "class_id": "class-mw"}]
                if with_objectives else []
            ),
            "enrollments": [
                {
                    "id": f"enr-{sid}",
                    "class_id": "class-mw",
                    "student_id": sid,
                    "muted": False,
                    "profiles": {"id": sid, "full_name": f"Student, {sid}", "role": "student", "email": f"{sid}@example.edu"},
                }
                for sid in students
            ],
            "grades": grades,
        }
        return _PagingClient(tables), students

    def test_1520_rows_are_complete_and_ignore_the_other_class(self):
        from app.models import Course
        from app.routes import _process_enrollments
        client, students = self._client(True)
        with unittest.mock.patch("app.models.supabase_admin", client):
            class_data = Course.get_full_class_data("class-mw")
        with unittest.mock.patch(
            "app.routes.objectives_attached_to_assignments",
            side_effect=lambda class_id, pool: list(pool or []),
        ):
            active, _, _ = _process_enrollments(class_data)
        self.assertEqual(len(active), 38)
        seen = set()
        for student in active:
            rows = []
            for enrollment in class_data["enrollments"]:
                prof = enrollment["profiles"]
                if prof["id"] == student["id"]:
                    rows = prof["grades"]
            self.assertEqual(len(rows), 40)
            for row in rows:
                self.assertEqual(row["learning_objective_id"], "lo-class")
                key = (row["student_id"], row["learning_objective_id"], row["assignment_id"])
                self.assertNotIn(key, seen)
                seen.add(key)
            self.assertEqual(len(student["learning_objectives"]), 1)
            self.assertEqual(student["learning_objectives"][0]["m_count"], 40)
        self.assertEqual(len(seen), 38 * 40)
        self.assertEqual(
            [page[0] for page in client.grade_pages],
            [("id",)] * len(client.grade_pages),
        )
        self.assertIn((("id",), 0, 999, 1520), client.grade_pages)
        self.assertIn((("id",), 1000, 1999, 1520), client.grade_pages)

    def test_more_than_1000_objectives_and_enrollments_are_kept(self):
        from app.models import Course
        los = [
            {
                "id": "lo-%04d" % index,
                "name": "L%04d" % index,
                "vendor_code": "L%04d" % index,
                "description": None,
                "required_ms": 2,
                "class_id": "class-mw",
            }
            for index in range(1001)
        ]
        enrollments = [
            {
                "id": "enr-%04d" % index,
                "class_id": "class-mw",
                "student_id": "stu-%04d" % index,
                "muted": False,
                "profiles": {
                    "id": "stu-%04d" % index,
                    "full_name": "Student, %04d" % index,
                    "role": "student",
                    "email": "s%04d@example.edu" % index,
                },
            }
            for index in range(1001)
        ]
        client = _PagingClient({
            "classes": [{"id": "class-mw", "name": "MW"}],
            "learning_objectives": los,
            "enrollments": enrollments,
            "grades": [],
        })
        with unittest.mock.patch("app.models.supabase_admin", client):
            class_data = Course.get_full_class_data("class-mw")
        self.assertEqual(len(class_data["learning_objectives"]), 1001)
        self.assertEqual(len(class_data["enrollments"]), 1001)

    def test_assignment_links_and_homework_scores_past_the_row_cap(self):
        from app.models import Course, Homework
        links = [
            {"assignment_id": "a1", "learning_objective_id": "lo-%04d" % index}
            for index in range(1001)
        ]
        scores = [
            {
                "id": "h-%04d" % index,
                "class_id": "c1",
                "student_id": "s-%04d" % index,
                "homework_group": "a1",
                "score_pct": 80,
            }
            for index in range(1001)
        ]
        client = _PagingClient({
            "assignment_objectives": links,
            "assignments": [{"id": "a1", "class_id": "c1", "homework_group": ""}],
            "homework_scores": scores,
        })
        with unittest.mock.patch("app.models.supabase_admin", client):
            lo_ids = Course.get_assignment_lo_ids("a1")
            hw_map = Homework.get_hw_scores_map_for_assignment("c1", "a1")
        self.assertEqual(len(lo_ids), 1001)
        self.assertEqual(len(hw_map), 1001)
        self.assertEqual(hw_map["s-0000"], 80)
        self.assertEqual(hw_map["s-1000"], 80)

    def test_zero_objectives_load_no_grades(self):
        from app.models import Course
        client, _students = self._client(False)
        with unittest.mock.patch("app.models.supabase_admin", client):
            class_data = Course.get_full_class_data("class-mw")
        self.assertEqual(client.grade_builds, 0)
        for enrollment in class_data["enrollments"]:
            self.assertEqual(enrollment["profiles"]["grades"], [])


class TestFullClassSelect(unittest.TestCase):
    def test_one_classes_select_omits_hw_pass_columns(self):
        from app.models import Course

        selects = []

        class _Result:
            def __init__(self, data):
                self.data = data

        class _Query:
            def __init__(self, name):
                self.name = name

            def select(self, cols, *args, **kwargs):
                selects.append((self.name, cols))
                return self

            def eq(self, *args, **kwargs):
                return self

            def in_(self, *args, **kwargs):
                return self

            def order(self, *args, **kwargs):
                return self

            def range(self, *args, **kwargs):
                return self

            def execute(self):
                if self.name == "classes":
                    return _Result([{
                        "id": "c1",
                        "name": "MW",
                        "semester": "Fall 2026",
                        "days": "MW",
                        "section_number": "F26",
                        "auto_convert_m": False,
                        "is_online": False,
                    }])
                return _Result([])

        class _Client:
            def table(self, name):
                return _Query(name)

        with unittest.mock.patch("app.models.supabase_admin", _Client()):
            class_data = Course.get_full_class_data("c1")

        class_selects = [cols for name, cols in selects if name == "classes"]
        self.assertEqual(len(class_selects), 1)
        self.assertNotIn("hw_passes_enabled", class_selects[0])
        self.assertNotIn("hw_passes_allowed", class_selects[0])
        self.assertEqual(
            class_selects[0],
            "id, name, semester, days, section_number, auto_convert_m, is_online",
        )
        self.assertEqual(class_data["name"], "MW")


class TestInsertLearningObjectives(unittest.TestCase):
    def _insert(self, payload, execute_error=None):
        from app import routes as r
        inserted = []

        class _Query:
            def insert(self, body):
                inserted.append(body)
                return self

            def execute(self):
                if execute_error is not None:
                    raise execute_error
                return type("R", (), {"data": [{"id": "lo-1"}]})()

        class _Client:
            def table(self, name):
                return _Query()

        with unittest.mock.patch.object(r, "supabase_admin", _Client()):
            result = r._insert_learning_objectives(payload)
        return inserted, result

    def test_missing_name_uses_vendor_code_on_the_only_insert(self):
        inserted, result = self._insert({
            "class_id": "c1",
            "vendor_code": "D1",
            "description": None,
        })
        self.assertEqual(len(inserted), 1)
        self.assertEqual(inserted[0]["name"], "D1")
        self.assertEqual(inserted[0]["vendor_code"], "D1")
        self.assertEqual(result.data[0]["id"], "lo-1")

    def test_given_name_is_kept(self):
        inserted, _result = self._insert({
            "class_id": "c1",
            "vendor_code": "D1",
            "name": "Lab 1",
        })
        self.assertEqual(len(inserted), 1)
        self.assertEqual(inserted[0]["name"], "Lab 1")
        self.assertEqual(inserted[0]["vendor_code"], "D1")

    def test_failed_insert_is_not_retried(self):
        from app import routes as r
        inserted = []

        class _Query:
            def insert(self, body):
                inserted.append(body)
                return self

            def execute(self):
                raise RuntimeError("db down")

        class _Client:
            def table(self, name):
                return _Query()

        with unittest.mock.patch.object(r, "supabase_admin", _Client()):
            with self.assertRaises(RuntimeError):
                r._insert_learning_objectives({
                    "class_id": "c1",
                    "vendor_code": "D1",
                })
        self.assertEqual(len(inserted), 1)


class TestStudentProgressCardRules(unittest.TestCase):
    def _card(self, count):
        from types import SimpleNamespace
        objectives = [
            SimpleNamespace(
                is_passed=False,
                vendor_code="T%s" % index,
                name="T%s" % index,
                grades_list=[],
                grades_meta=[],
                m_count=0,
                required_ms=2,
                mr_count=0,
            )
            for index in range(count)
        ]
        student = SimpleNamespace(
            learning_objectives=objectives,
            objective_total=count,
        )
        app = create_app()
        with app.app_context():
            template = app.jinja_env.get_template("_components.html")
            return template.module.student_progress_cards(student)

    def test_rule_count_is_one_less_than_objectives(self):
        self.assertEqual(self._card(1).count("lo-row-rule"), 0)
        self.assertEqual(self._card(4).count("lo-row-rule"), 3)


class TestClassObjectiveTotal(unittest.TestCase):
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

    def _render_card(self, student):
        app = create_app()
        with app.app_context():
            template = app.jinja_env.get_template("_components.html")
            return template.module.student_progress_cards(student)

    def test_no_grades_card_is_zero_of_n(self):
        from app.routes import _process_enrollments
        active, _, _ = _process_enrollments(self._class_data(self._pool(4), []))
        student = active[0]
        self.assertEqual(student["objective_total"], 4)
        self.assertEqual(student["learning_objectives"], [])
        html = self._render_card(student)
        self.assertIn("0 / 4", html)
        self.assertEqual(html.count("lo-row"), 0)

    def test_partial_grades_use_class_total(self):
        from app.routes import _process_enrollments
        active, _, _ = _process_enrollments(
            self._class_data(self._pool(3), self._passing("lo-0"))
        )
        student = active[0]
        self.assertEqual(len(student["learning_objectives"]), 1)
        self.assertEqual(student["objective_total"], 3)
        passed = sum(1 for row in student["learning_objectives"] if row["is_passed"])
        self.assertEqual(passed, 1)
        html = self._render_card(student)
        self.assertIn("1 / 3", html)

    def test_empty_class_pool_is_zero_of_zero(self):
        from app.routes import _process_enrollments
        active, _, _ = _process_enrollments(self._class_data([], []))
        student = active[0]
        self.assertEqual(student["objective_total"], 0)
        self.assertEqual(student["learning_objectives"], [])
        html = self._render_card(student)
        self.assertIn("0 / 0", html)

    def test_outside_pool_grade_does_not_raise_passed(self):
        from app.routes import class_lo_lookup, class_progress
        rows, total = class_progress(
            self._passing("lo-0") + self._passing("other"),
            class_lo_lookup(self._pool(1)),
        )
        passed = sum(1 for row in rows if row["is_passed"])
        self.assertEqual(total, 1)
        self.assertEqual(passed, 1)
        self.assertLessEqual(passed, total)
        self.assertEqual([row["learning_objective_id"] for row in rows], ["lo-0"])

    def test_students_page_shows_zero_of_n(self):
        from flask import render_template, session
        app = create_app()
        student = {
            "id": "s1",
            "name": "Lee, Ana",
            "email": "",
            "learning_objectives": [],
            "objective_total": 4,
        }
        with app.test_request_context("/class/c1/students"):
            session["role"] = "instructor"
            html = render_template(
                "class_students.html",
                class_id="c1",
                class_name="MW",
                students=[student],
            )
        self.assertIn("0 / 4 objectives passed", html)

    def test_enrollment_and_detail_share_total(self):
        from app.routes import _process_enrollments, class_lo_lookup, class_progress
        pool = self._pool(2)
        grades = []
        active, _, _lookup = _process_enrollments(self._class_data(pool, grades))
        _rows, total = class_progress(grades, class_lo_lookup(pool))
        self.assertEqual(active[0]["objective_total"], total)
        self.assertEqual(total, 2)

    def test_lookup_skips_missing_id_and_empty_progress(self):
        from app.routes import class_lo_lookup, class_progress
        lookup = class_lo_lookup([
            {"id": "keep", "name": "Keep", "vendor_code": "K", "required_ms": 2},
            {"name": "No id"},
        ])
        self.assertEqual(set(lookup), {"keep"})
        grades = self._passing("keep")
        self.assertEqual(class_progress(grades, {}), ([], 0))


class TestLoSortKey(unittest.TestCase):
    def _codes(self, rows):
        from app.lo_order import lo_sort_key
        return [row["vendor_code"] for row in sorted(rows, key=lo_sort_key)]

    def test_letter_prefix_then_number(self):
        rows = [
            {"id": "4", "vendor_code": "CO1"},
            {"id": "3", "vendor_code": "AO10"},
            {"id": "1", "vendor_code": "AO1"},
            {"id": "2", "vendor_code": "AO2"},
        ]
        self.assertEqual(self._codes(rows), ["AO1", "AO2", "AO10", "CO1"])

    def test_mixed_case(self):
        rows = [
            {"id": "1", "vendor_code": "ao10"},
            {"id": "2", "vendor_code": "AO2"},
            {"id": "3", "vendor_code": "co1"},
        ]
        self.assertEqual(self._codes(rows), ["AO2", "ao10", "co1"])

    def test_whitespace_does_not_change_number_order(self):
        rows = [
            {"id": "1", "vendor_code": "A10"},
            {"id": "2", "vendor_code": "A 2"},
            {"id": "3", "vendor_code": "LO 12"},
            {"id": "4", "vendor_code": "LO2"},
        ]
        self.assertEqual(self._codes(rows), ["A 2", "A10", "LO2", "LO 12"])

    def test_dotted_numbers(self):
        rows = [
            {"id": "1", "vendor_code": "Test 1.10"},
            {"id": "2", "vendor_code": "Test 1.2"},
        ]
        self.assertEqual(self._codes(rows), ["Test 1.2", "Test 1.10"])

    def test_label_without_digits_is_alphabetical(self):
        rows = [
            {"id": "1", "vendor_code": "Keep"},
            {"id": "2", "vendor_code": "Alpha"},
        ]
        self.assertEqual(self._codes(rows), ["Alpha", "Keep"])

    def test_missing_label_sorts_last(self):
        from app.lo_order import lo_sort_key
        rows = [
            {"id": "1"},
            {"id": "2", "vendor_code": "AO1"},
            {"id": "3", "vendor_code": "  ", "name": ""},
        ]
        self.assertEqual(
            [row["id"] for row in sorted(rows, key=lo_sort_key)],
            ["2", "1", "3"],
        )

    def test_equal_labels_tie_break_on_id(self):
        from app.lo_order import lo_sort_key
        rows = [
            {"id": "b", "vendor_code": "AO1"},
            {"id": "a", "vendor_code": "AO1"},
        ]
        self.assertEqual(
            [row["id"] for row in sorted(rows, key=lo_sort_key)],
            ["a", "b"],
        )


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
        from app.routes import class_learning_objectives_summary
        app = create_app()
        with app.test_request_context("/class/c1/learning-objectives"):
            session["user_id"] = "u1"
            session["role"] = "instructor"
            with patch("app.routes._instructor_owns_class", return_value=True), \
                 patch("app.routes.Course.get_full_class_data", return_value=self._class_data(pool, grades)):
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
        from app.routes import lo_cell_status
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
        from app.routes import class_learning_objectives_summary
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
            with patch("app.routes._instructor_owns_class", return_value=True), \
                 patch("app.routes.Course.get_full_class_data", return_value=class_data):
                html = class_learning_objectives_summary("c1")
        self.assertIn(">passed</span>", html)
        self.assertRegex(html, r"text-yellow-700[^>]*>\s*2/3")
        self.assertIsNotNone(re.search(r"text-green-700[\s\S]*?✓", html))
        self.assertIn('title="2/2"', html)
        self.assertIn('title="2/3"', html)

    def test_helper_text_matches_the_page(self):
        from flask import session
        from unittest.mock import patch
        from app.routes import (
            class_learning_objectives_summary,
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
            with patch("app.routes._instructor_owns_class", return_value=True), \
                 patch("app.routes.Course.get_full_class_data", return_value=class_data):
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
        from app.routes import add_class
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
            with patch("app.routes.ensure_profile_exists"), \
                 patch("app.routes.supabase_admin") as sb:
                sb.table.return_value.insert.side_effect = insert
                result = add_class()
        return result, saved.get("payload")

    def _copy(self, section, token, days="MW"):
        from flask import session
        from unittest.mock import MagicMock, patch
        from app.routes import copy_class
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
            with patch("app.routes._instructor_owns_class", return_value=True), \
                 patch("app.routes._select_class_pool_learning_objectives", return_value=lo_resp), \
                 patch("app.routes.supabase_admin") as sb:
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
        from app.routes import CLASS_DAYS
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
        from app.routes import add_class
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
            with patch("app.routes.ensure_profile_exists"), \
                 patch("app.routes.supabase_admin") as sb:
                sb.table.return_value.insert.side_effect = insert
                result = add_class()
        self.assertEqual(result[1], 500)
        self.assertEqual(result[0], "Failed to create class.")
        self.assertEqual(calls["n"], 1)

    def test_copy_schema_error_is_one_insert(self):
        from flask import session
        from unittest.mock import MagicMock, patch
        from app.routes import copy_class
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
            with patch("app.routes._instructor_owns_class", return_value=True), \
                 patch("app.routes._select_class_pool_learning_objectives", return_value=lo_resp), \
                 patch("app.routes.supabase_admin") as sb:
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
        from app.routes import student_history
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
            with patch("app.routes._instructor_owns_class", return_value=True), \
                 patch("app.routes.Course.get_full_class_data", return_value={"name": "MW", "learning_objectives": []}), \
                 patch("app.routes.supabase_admin") as sb:
                sb.table.return_value.select.side_effect = select
                result = student_history("c1", "s1")
        self.assertEqual(result.status_code, 302)
        self.assertTrue(result.headers["Location"].endswith("/class/c1/reports"))
        self.assertEqual(selects, ["student_id, profiles(id, full_name, email)"])


class TestStudentHistoryOrder(unittest.TestCase):
    def _render_history(self, grades, assignments):
        import json
        from flask import session
        from unittest.mock import MagicMock, patch
        from app.routes import student_history

        def table(name):
            chain = MagicMock()
            if name == "enrollments":
                chain.select.return_value.eq.return_value.eq.return_value.single.return_value.execute.return_value.data = {
                    "student_id": "s1",
                    "profiles": {"id": "p1", "full_name": "Lee, Ana", "email": "a@b.edu"},
                }
            elif name == "grades":
                chain.select.return_value.eq.return_value.execute.return_value.data = grades
            return chain

        app = create_app()
        with app.test_request_context("/class/c1/student/s1/history"):
            session["user_id"] = "u1"
            session["role"] = "instructor"
            with patch("app.routes._instructor_owns_class", return_value=True), \
                 patch("app.routes.Course.get_full_class_data", return_value={
                     "name": "Test Upload",
                     "days": "MW",
                     "section_number": "F26",
                     "learning_objectives": [{
                         "id": "lo1",
                         "vendor_code": "D1",
                         "description": "",
                     }],
                 }), \
                 patch("app.routes.objectives_attached_to_assignments", side_effect=lambda class_id, pool: list(pool or [])), \
                 patch("app.routes.load_assignments_for_class", return_value=assignments) as load_assignments, \
                 patch("app.routes._report_sender_name", return_value="Estes"), \
                 patch("app.routes.supabase_admin") as sb:
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
            os.path.dirname(__file__), "..", "app", "templates", "student_history.html",
        )
        table_path = os.path.join(
            os.path.dirname(__file__), "..", "app", "templates", "student_history_table.html",
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

    def test_empty_assignment_list_still_renders_every_grade(self):
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
        self.assertEqual(names, ["Quiz", "Later", "Assignment"])
        self.assertEqual(scores, ["M", "R", "X"])
        self.assertEqual(self._print_rows(html), [])
        self.assertEqual(
            [grade.get("assignment_id") for grade in emailed],
            ["a-early", "a-late", None],
        )
        self.assertEqual([grade.get("top_score") for grade in emailed], ["M", "R", "X"])

    def test_missing_assignment_id_sits_between_listed_and_null_grades(self):
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
        self.assertEqual(names, ["Quiz", "Outside", "Assignment"])
        self.assertEqual(scores, ["M", "P", "X"])
        self.assertEqual(self._print_rows(html), [("Quiz", "M")])
        self.assertEqual(
            [grade.get("assignment_id") for grade in emailed],
            ["a-early", "z-out", None],
        )
        self.assertEqual([grade.get("top_score") for grade in emailed], ["M", "P", "X"])

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
        from app.routes import student_history_print

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
            with patch("app.routes.supabase_admin", admin), \
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
                "class_reports.html",
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
        from app.routes import _history_packet_grades

        chain = MagicMock()
        chain.select.return_value = chain
        chain.in_.return_value = chain
        chain.order.return_value = chain
        chain.range.return_value = chain
        chain.execute.return_value = MagicMock(data=[])
        admin = MagicMock()
        admin.table.return_value = chain
        with patch("app.routes.supabase_admin", admin):
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
        from app.routes import student_history_print

        app = create_app()
        client = app.test_client()
        signed_out = client.get("/class/c1/student-history/print")
        self.assertEqual(signed_out.status_code, 302)
        self.assertTrue(signed_out.headers["Location"].endswith("/login"))
        with app.test_request_context("/class/c1/student-history/print"):
            session["user_id"] = "prof-1"
            session["role"] = "instructor"
            with patch("app.routes._instructor_owns_class", return_value=False):
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
        root = os.path.join(os.path.dirname(__file__), "..", "app", "templates")
        with open(os.path.join(root, "student_history_print.html"), encoding="utf-8") as handle:
            batch = handle.read()
        with open(os.path.join(root, "student_history.html"), encoding="utf-8") as handle:
            page = handle.read()
        with open(os.path.join(root, "student_history_table.html"), encoding="utf-8") as handle:
            table = handle.read()
        self.assertIn("@page { size: letter portrait; margin: 0; }", batch)
        self.assertIn("print-color-adjust: exact", batch)
        self.assertIn("break-after: page", batch)
        self.assertIn("break-inside: avoid", batch)
        self.assertIn("}, 250);", batch)
        self.assertIn('{% include "student_history_table.html" %}', page)
        self.assertIn('{% include "student_history_table.html" %}', batch)
        self.assertLess(table.find("<thead>"), table.find("history-sheet-name"))
        self.assertLess(table.find("history-sheet-name"), table.find("</thead>"))
        self.assertIn("history-print-spacer", table)
        self.assertIn('class="history-col-assignment"', table)
        self.assertIn("{% for cell in row.cells %}", table)
        self.assertNotIn("namespace(", table)

    def test_history_table_css_lives_in_one_file(self):
        root = os.path.join(os.path.dirname(__file__), "..", "app")
        with open(os.path.join(root, "static", "css", "student_history_table.css"), encoding="utf-8") as handle:
            css = handle.read()
        with open(os.path.join(root, "templates", "student_history.html"), encoding="utf-8") as handle:
            page = handle.read()
        with open(os.path.join(root, "templates", "student_history_print.html"), encoding="utf-8") as handle:
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
        from app.routes import student_history_print

        source = inspect.getsource(inspect.unwrap(student_history_print))
        self.assertNotIn("request.args", source)

    def test_batch_and_single_pages_render_the_same_table(self):
        from flask import session
        from unittest.mock import patch
        from app.routes import student_history, student_history_print

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
            with patch("app.routes.supabase_admin", admin), \
                 patch("app.models.supabase_admin", admin):
                single = student_history("c1", "s-adams")
        with app.test_request_context("/class/c1/student-history/print"):
            session["user_id"] = "prof-1"
            session["role"] = "instructor"
            with patch("app.routes.supabase_admin", admin), \
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


class TestClassDisplayTitle(unittest.TestCase):
    def test_name_days_and_section(self):
        from flask import render_template, session
        from app.routes import CLASS_DAYS, class_display_title
        title = class_display_title("Test Upload", "MW", "F26")
        self.assertEqual(title, "Test Upload | MW | F26")
        app = create_app()
        with app.test_request_context("/instructor/dashboard"):
            session["role"] = "instructor"
            html = render_template(
                "instructor_select_class.html",
                classes=[{
                    "id": "c1",
                    "name": "Test Upload",
                    "days": "MW",
                    "section_number": "F26",
                    "semester": "Fall 2026",
                    "is_online": False,
                }],
                create_class_token="t",
                copy_class_token="c",
                class_days=CLASS_DAYS,
            )
        self.assertIn(title, html)
        self.assertIn("Fall 2026", html)
        self.assertIn("In Person", html)

    def test_blank_section_has_no_trailing_separator(self):
        from app.routes import class_display_title
        self.assertEqual(class_display_title("Test Upload", "MW", "  "), "Test Upload | MW")
        self.assertEqual(class_display_title("Test Upload", "MW", None), "Test Upload | MW")

    def test_blank_days_and_section_is_name_only(self):
        from app.routes import class_display_title
        self.assertEqual(class_display_title("  Test Upload  ", "", None), "Test Upload")

    def test_archive_button_carries_the_escaped_raw_name(self):
        from flask import render_template, session
        from app.routes import CLASS_DAYS
        app = create_app()
        with app.test_request_context("/instructor/dashboard"):
            session["role"] = "instructor"
            html = render_template(
                "instructor_select_class.html",
                classes=[
                    {
                        "id": "c1",
                        "name": 'Algebra "A" <b>',
                        "days": "MW",
                        "section_number": "1",
                        "semester": "Fall 2026",
                        "is_online": False,
                    },
                    {
                        "id": "c2",
                        "name": "Geometry",
                        "days": "TTH",
                        "section_number": "2",
                        "semester": "Fall 2026",
                        "is_online": False,
                    },
                ],
                create_class_token="t",
                copy_class_token="c",
                class_days=CLASS_DAYS,
            )
        buttons = [
            line.strip()
            for line in html.splitlines()
            if "<button" in line and "archive-class-btn" in line
        ]
        self.assertEqual(len(buttons), 2)
        self.assertIn('data-class-id="c1"', buttons[0])
        self.assertIn('data-class-id="c2"', buttons[1])
        self.assertIn('data-class-name="Geometry"', buttons[1])
        escaped = 'data-class-name="Algebra &#34;A&#34; &lt;b&gt;"'
        self.assertIn(escaped, buttons[0])
        self.assertNotIn("<b>", buttons[0])
        self.assertNotIn('data-class-name="Algebra "', buttons[0])


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
        from app.routes import update_class
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
            with patch("app.routes._instructor_owns_class", return_value=owns), \
                 patch("app.routes.supabase_admin") as sb:
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
        from app.routes import CLASS_DAYS, class_display_title
        title = class_display_title("Renamed", "TTH", None)
        self.assertEqual(title, "Renamed | TTH")
        app = create_app()
        with app.test_request_context("/instructor/dashboard"):
            session["role"] = "instructor"
            html = render_template(
                "instructor_select_class.html",
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
        from app.routes import CLASS_DAYS
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
                "instructor_select_class.html",
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


class TestClassHeaderTitle(unittest.TestCase):
    def _render(self, template, **kwargs):
        from flask import render_template, session
        app = create_app()
        with app.test_request_context("/"):
            session["role"] = "instructor"
            return render_template(template, **kwargs)

    def _assert_title(self, html, section):
        if section:
            self.assertIn("Test Upload | MW | F26", html)
        else:
            self.assertIn("Test Upload | MW", html)
            self.assertNotIn("Test Upload | MW |", html)

    def _card(self, section):
        from app.routes import CLASS_DAYS
        return self._render(
            "instructor_select_class.html",
            classes=[{
                "id": "c1",
                "name": "Test Upload",
                "days": "MW",
                "section_number": section,
                "semester": "Fall 2026",
                "is_online": False,
            }],
            create_class_token="t",
            copy_class_token="c",
            class_days=CLASS_DAYS,
        )

    def _dashboard(self, section):
        from datetime import date
        from app.routes import build_revision_report
        return self._render(
            "class_detail.html",
            class_id="c1",
            class_name="Test Upload",
            class_days="MW",
            class_section=section,
            students=[],
            all_students=[],
            learning_objectives=[],
            assignments=[],
            revision_report=build_revision_report([], date(2026, 10, 20)),
            revision_printed_on="2026-10-20",
        )

    def test_dashboard_header_matches_card_title(self):
        for section in ("F26", None):
            card = self._card(section)
            dashboard = self._dashboard(section)
            self._assert_title(card, section)
            self._assert_title(dashboard, section)

    def test_students_summary_and_detail_headers(self):
        student = {
            "id": "s1",
            "name": "Lee, Ana",
            "email": "",
            "learning_objectives": [],
            "objective_total": 0,
        }
        pages = (
            (
                "class_students.html",
                {"class_id": "c1", "students": [student]},
            ),
            (
                "class_learning_objectives_summary.html",
                {
                    "class_id": "c1",
                    "learning_objectives": [],
                    "grid_rows": [],
                    "total_students": 0,
                },
            ),
            (
                "class_student_detail.html",
                {"class_id": "c1", "student": student},
            ),
        )
        for template, extra in pages:
            for section in ("F26", None):
                html = self._render(
                    template,
                    class_name="Test Upload",
                    class_days="MW",
                    class_section=section,
                    **extra,
                )
                self._assert_title(html, section)

    def test_print_mastery_badge_sizes_to_its_text(self):
        html = self._render(
            "class_student_detail.html",
            class_id="c1",
            class_name="Test Upload",
            class_days="MW",
            class_section="F26",
            student={
                "id": "s1",
                "name": "Lee, Ana",
                "email": "",
                "learning_objectives": [],
                "objective_total": 0,
            },
        )
        start = html.find(".roster-print-student .mastery-badge,")
        self.assertGreaterEqual(start, 0)
        block = html[start:html.find("}", start)]
        self.assertIn("width: auto", block)
        self.assertIn("min-width: max-content", block)
        self.assertIsNone(re.search(r"width:\s*\d", block))

    def test_history_email_and_report_print_keep_raw_name(self):
        history = self._render(
            "student_history.html",
            class_id="c1",
            student_id="s1",
            student_name="Lee, Ana",
            student_email="a@b.edu",
            instructor_title="Professor Estes",
            history_assignments=[],
            learning_objectives=[],
            class_name="Test Upload",
            class_days="MW",
            class_section="F26",
        )
        self.assertIn("Test Upload | MW | F26", history)
        self.assertIn('"class_name": "Test Upload"', history)

    def test_history_mr_score_renders_as_mastery_badge(self):
        html = self._render(
            "student_history.html",
            class_id="c1",
            student_id="s1",
            student_name="Lee, Ana",
            student_email="",
            instructor_title="Professor Estes",
            class_name="Test Upload",
            class_days="MW",
            class_section="F26",
            history_assignments=[],
            learning_objectives=[
                {
                    "vendor_code": "D1",
                    "grades": [
                        {
                            "top_score": "MR",
                            "second_score": None,
                            "assignments": {"name": "Quiz 1"},
                        }
                    ],
                }
            ],
        )
        self.assertIn("mastery-badge mastery-mr", html)
        self.assertNotRegex(html, r'student-history-numeric[^>]*>\s*MR\s*<')

        reports = self._render(
            "class_reports.html",
            class_id="c1",
            class_name="Test Upload",
            class_days="MW",
            class_section="F26",
            students=[],
            learning_objectives=[],
            assignments=[],
            instructor_title="Professor Estes",
        )
        self.assertEqual(reports.count("Test Upload | MW | F26"), 2)
        self.assertIn('mt-0.5">Test Upload</p>\';', reports)

    def test_reports_prev_column_matches_every_row_for_screen_print_and_pdf(self):
        from html.parser import HTMLParser

        class _Rows(HTMLParser):
            def __init__(self):
                super().__init__()
                self.capture = False
                self.rows = []
                self.cur = None

            def handle_starttag(self, tag, attrs):
                ad = dict(attrs)
                if tag == "table" and ad.get("id") == "all-students-table":
                    self.capture = True
                if not self.capture:
                    return
                if tag == "tr":
                    self.cur = []
                if tag in ("th", "td") and self.cur is not None:
                    self.cur.append(ad)

            def handle_endtag(self, tag):
                if not self.capture:
                    return
                if tag == "tr" and self.cur is not None:
                    self.rows.append(self.cur)
                    self.cur = None
                if tag == "table":
                    self.capture = False

        html = self._render(
            "class_reports.html",
            class_id="c1",
            class_name="Test Upload",
            class_days="MW",
            class_section="F26",
            students=[
                {"id": "s1", "name": "Lee, Ana", "email": "a@b.edu"},
                {"id": "s2", "name": "Kim, Bo", "email": ""},
            ],
            learning_objectives=[{"id": "lo1", "vendor_code": "D1"}],
            assignments=[],
            instructor_title="Professor Estes",
        )
        parser = _Rows()
        parser.feed(html)
        header = parser.rows[0]
        prev_headers = [cell for cell in header if "data-hw-prev-col-header" in cell]
        self.assertEqual(len(prev_headers), 1)
        self.assertEqual(
            [cell for cell in header if "data-hw-col-header" in cell],
            [header[header.index(prev_headers[0]) + 1]],
        )
        body_rows = parser.rows[1:]
        self.assertEqual(len(body_rows), 2)
        for row in body_rows:
            self.assertEqual(len(row), len(header))
            self.assertEqual(sum(1 for cell in row if "data-hw-prev-cell" in cell), 1)
        printable = [
            [cell for cell in row if "data-email-col" not in cell]
            for row in parser.rows
        ]
        for row in printable[1:]:
            self.assertEqual(len(row), len(printable[0]))
            self.assertEqual(sum(1 for cell in row if "data-hw-prev-cell" in cell), 1)
        self.assertEqual(sum(1 for cell in printable[0] if "data-hw-prev-col-header" in cell), 1)
        self.assertEqual(html.count("HW % prev"), 2)
        self.assertIn("function formatHwPrevScore", html)
        self.assertIn("function exportGradeSheetPdf", html)
        self.assertIn("data-hw-prev-col-header", html)
        self.assertIn("assignmentHwPrev", html)
        self.assertIn("columnStyles", html)
        self.assertIn(".hw-prev-score-display", html)
        self.assertNotIn("th:nth-child(2)", html)
        self.assertIn("function applySinglePagePrintScale", html)
        self.assertIn("pageHeightPx", html)


class _RemovalQuery:
    def __init__(self, log, table, rows, fail_delete, fail_insert=None, fail_update=None, fail_select=None):
        self.log = log
        self.table = table
        self.rows = list(rows)
        self.fail_delete = fail_delete
        self.fail_insert = fail_insert
        self.fail_update = fail_update
        self.fail_select = fail_select
        self.op = None
        self.filters = []

    def select(self, *args, **kwargs):
        if kwargs.get("count") == "exact" and kwargs.get("head"):
            self.op = "count"
        else:
            self.op = "select"
        return self

    def delete(self):
        self.op = "delete"
        return self

    def insert(self, rows):
        self.op = "insert"
        self.inserted = rows
        return self

    def update(self, payload):
        self.op = "update"
        self.updated = payload
        return self

    def eq(self, column, value):
        self.filters.append(("eq", column, value))
        return self

    def in_(self, column, values):
        self.filters.append(("in", column, list(values)))
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def range(self, start, end):
        return self

    def execute(self):
        if self.op == "update" and self.fail_update == self.table:
            self.log.append({
                "table": self.table,
                "op": "update-failed",
                "filters": list(self.filters),
            })
            raise RuntimeError("update failed")
        if self.op == "insert" and self.fail_insert == self.table:
            self.log.append({
                "table": self.table,
                "op": "insert-failed",
                "filters": list(self.filters),
                "rows": self.inserted,
            })
            raise RuntimeError("insert failed")
        if self.op == "delete" and self.fail_delete == self.table:
            self.log.append({
                "table": self.table,
                "op": "delete-failed",
                "filters": list(self.filters),
            })
            raise RuntimeError("delete failed")
        if self.op == "select" and self.fail_select == self.table:
            self.log.append({
                "table": self.table,
                "op": "select-failed",
                "filters": list(self.filters),
            })
            raise RuntimeError("select failed")
        entry = {
            "table": self.table,
            "op": self.op,
            "filters": list(self.filters),
        }
        if self.op == "insert":
            entry["rows"] = self.inserted
        self.log.append(entry)
        count = len(self.rows) if self.op == "count" else None
        data = self.rows if self.op == "select" else []
        return unittest.mock.MagicMock(data=data, count=count)


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
        from app import routes as r
        client = self._client()
        sa = self._removal_sa(log, fail_delete=fail_delete)
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=owns), \
                unittest.mock.patch.object(r, "_student_enrolled_in_class", return_value=enrolled), \
                unittest.mock.patch.object(r.Course, "get_all_lo_ids_for_class", return_value=["lo-this"]):
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
        from app import routes as r
        audit_rows = [
            row
            for entry in log
            if entry.get("table") == "grade_change_log" and entry.get("op") == "insert"
            for row in (entry.get("rows") or [])
        ]
        self.assertTrue(audit_rows)
        self.assertTrue(all(row["operation"] == r.GRADE_LOG_DELETE for row in audit_rows))
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
        from app import routes as r
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
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
        from app import routes as r
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
        from app.routes import build_revision_report
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
                "class_detail.html",
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


class TestFreePassAllowance(unittest.TestCase):
    def _post(self, path, rows):
        from app import routes as r
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
        from app.routes import FREE_PASSES_ALLOWED
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
        from app.routes import FREE_PASSES_ALLOWED
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
        from app.routes import FREE_PASSES_ALLOWED
        rv, log = self._post(
            "/api/class/c1/return_pass",
            [{"id": "fp1", "passes_used": 1}],
        )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.get_json()["passes_remaining"], FREE_PASSES_ALLOWED)
        updated = [entry for entry in log if entry["op"] == "update"]
        self.assertEqual(len(updated), 1)
        self.assertEqual(updated[0]["payload"], {"passes_used": 0})


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
        from app import routes as r
        log = []

        def table(name):
            return _RemovalQuery(log, name, rows_for.get(name, []), None)

        client = self._client()
        with unittest.mock.patch.object(r, "supabase_admin") as sa, \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=owns):
            sa.table.side_effect = table
            rv = client.post(path, headers={"X-CSRF-Token": "test-csrf"})
        return rv, log

    def _run_rows(self, rows_for, fail_delete=None, fail_select=None):
        from app import routes as r
        log = []
        self._log = log

        def table(name):
            return _RemovalQuery(
                log, name, rows_for.get(name, []), fail_delete, fail_select=fail_select
            )

        sa = unittest.mock.MagicMock()
        sa.table.side_effect = table
        with unittest.mock.patch.object(r, "supabase_admin", sa):
            r.delete_class_rows("c1", "inst1")
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


class TestAttachedObjectives(unittest.TestCase):
    def _clear_cache(self):
        from app import routes as r
        r._request_cache().clear()

    def _passing(self, lo_id):
        return [
            {"learning_objective_id": lo_id, "top_score": "M", "counts_for_mastery": True},
            {"learning_objective_id": lo_id, "top_score": "M", "counts_for_mastery": True},
        ]

    def _run(self, class_id, pool, grades, link_rows, students=1):
        from app import routes as r
        calls = []

        class _Query:
            def __init__(self, name):
                self.name = name
                self.filters = []

            def select(self, *args, **kwargs):
                return self

            def eq(self, column, value):
                self.filters.append(("eq", column, value))
                return self

            def order(self, *args, **kwargs):
                return self

            def range(self, start, end):
                return self

            def execute(self):
                calls.append({"table": self.name, "filters": list(self.filters)})
                rows = link_rows if self.name == "assignment_objectives" else []
                return unittest.mock.MagicMock(data=rows)

        enrollments = []
        for index in range(students):
            enrollments.append({
                "muted": False,
                "profiles": {
                    "id": "s%s" % index,
                    "full_name": "Student, %s" % index,
                    "email": "s%s@b.edu" % index,
                    "grades": list(grades),
                },
            })
        class_data = {
            "id": class_id,
            "learning_objectives": pool,
            "enrollments": enrollments,
        }
        self._clear_cache()
        try:
            with unittest.mock.patch.object(r, "supabase_admin") as sa:
                sa.table.side_effect = lambda name: _Query(name)
                active, _, _ = r._process_enrollments(class_data)
                attached = r.objectives_attached_to_assignments(class_id, pool)
        finally:
            self._clear_cache()
        ao_calls = [row for row in calls if row["table"] == "assignment_objectives"]
        return active, attached, ao_calls

    def test_legacy_unattached_grades_stay_within_the_attached_total(self):
        pool = [
            {"id": "lo-0", "name": "LO 0", "vendor_code": "T0", "required_ms": 2},
            {"id": "lo-1", "name": "LO 1", "vendor_code": "T1", "required_ms": 2},
            {"id": "lo-2", "name": "LO 2", "vendor_code": "T2", "required_ms": 2},
        ]
        grades = self._passing("lo-0") + self._passing("lo-1") + self._passing("lo-2")
        links = [
            {"assignment_id": "a1", "learning_objective_id": "lo-0"},
            {"assignment_id": "a2", "learning_objective_id": "lo-0"},
        ]
        active, attached, ao_calls = self._run("c-attached", pool, grades, links, students=3)
        self.assertEqual(len(ao_calls), 1)
        self.assertIn(("eq", "assignments.class_id", "c-attached"), ao_calls[0]["filters"])
        self.assertEqual([lo["id"] for lo in attached], ["lo-0"])
        self.assertEqual(len(active), 3)
        for student in active:
            passed = sum(1 for row in student["learning_objectives"] if row["is_passed"])
            self.assertEqual(student["objective_total"], 1)
            self.assertEqual(passed, 1)
            self.assertLessEqual(passed, student["objective_total"])
            self.assertEqual(
                [row["learning_objective_id"] for row in student["learning_objectives"]],
                ["lo-0"],
            )

    def test_no_assignment_links_leave_passed_and_total_at_zero(self):
        pool = [
            {"id": "lo-0", "name": "LO 0", "vendor_code": "T0", "required_ms": 2},
        ]
        active, attached, ao_calls = self._run(
            "c-none", pool, self._passing("lo-0"), [], students=3
        )
        self.assertEqual(len(ao_calls), 1)
        self.assertEqual(attached, [])
        for student in active:
            passed = sum(1 for row in student["learning_objectives"] if row["is_passed"])
            self.assertEqual(student["objective_total"], 0)
            self.assertEqual(passed, 0)
            self.assertLessEqual(passed, student["objective_total"])

    def test_assignments_page_states_the_unattached_count_outside_the_list(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "templates", "class_assignments.html"
        )
        with open(path, encoding="utf-8") as handle:
            src = handle.read()
        message = src.index("not attached to an assignment yet")
        wrapper = src.index('id="assignmentsExistingLosListWrapper"')
        self.assertLess(message, wrapper)
        self.assertIn("{% if unattached_count %}", src)

    def test_learning_objectives_card_links_to_the_summary_page(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "templates", "class_detail.html"
        )
        with open(path, encoding="utf-8") as handle:
            src = handle.read()
        self.assertIn("total_los = learning_objectives|length", src)
        self.assertIn("main.class_learning_objectives_summary", src)

    def test_save_confirms_only_when_removed_objectives_have_grades(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "templates", "class_assignments.html"
        )
        with open(path, encoding="utf-8") as handle:
            src = handle.read()
        start = src.index("function saveAssignment()")
        end = src.index("\nfunction downloadBlankCsv")
        body = src[start:end]
        create_branch = body.index("if (!assignmentId)")
        count_fetch = body.index("detach-grade-count")
        dialog = body.index("if (gradeRows > 0)")
        self.assertIn("selected_lo=", body)
        self.assertLess(create_branch, count_fetch)
        self.assertNotIn("confirm(", body[create_branch:count_fetch])
        self.assertIn("postSave();", body[create_branch:count_fetch])
        self.assertLess(dialog, body.index("confirm(", dialog))
        self.assertIn("postSave();", body[dialog:])


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
        from app import routes as r
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
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=owns), \
                unittest.mock.patch.object(
                    r.Course, "get_lo_ids_for_class", return_value=["lo-1", "lo-2"]
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


class TestClassArchiveExport(unittest.TestCase):
    S1 = "aaaaaaaa-1111-1111-1111-111111111111"
    S2 = "bbbbbbbb-2222-2222-2222-222222222222"
    S3 = "cccccccc-3333-3333-3333-333333333333"
    S4 = "dddddddd-4444-4444-4444-444444444444"
    ASG1 = "11111111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    ASG2 = "22222222-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    def _clear_attached_cache(self):
        from app import routes as routes_module
        routes_module._request_cache().pop(("_attached_objective_ids", "c-archive"), None)

    def _class_data(self):
        def marks(lo_id, count):
            return [
                {
                    "learning_objective_id": lo_id,
                    "top_score": "M",
                    "counts_for_mastery": True,
                }
                for _index in range(count)
            ]

        people = [
            (self.S3, "Doe/Jane, Ann", "doe@example.edu", False, []),
            (self.S1, "Lee, Ana", "hidden@example.edu", False, marks("lo-1", 2) + marks("lo-9", 2)),
            (self.S2, "Lee, Ana", "lee2@example.edu", False, []),
            (self.S4, "Muted, Max", "secret@example.edu", True, marks("lo-1", 1)),
        ]
        return {
            "id": "c-archive",
            "name": "Archive Class",
            "learning_objectives": [
                {"id": "lo-1", "name": "D1", "vendor_code": "D1", "required_ms": 2},
                {"id": "lo-9", "name": "ZZZ", "vendor_code": "ZZZ", "required_ms": 2},
            ],
            "enrollments": [
                {
                    "muted": muted,
                    "profiles": {
                        "id": sid,
                        "full_name": name,
                        "email": email,
                        "grades": grades,
                    },
                }
                for sid, name, email, muted, grades in people
            ],
        }

    def _links(self):
        return [
            {"assignment_id": self.ASG1, "learning_objective_id": "lo-1"},
            {"assignment_id": self.ASG2, "learning_objective_id": "lo-1"},
        ]

    def _assignments(self):
        return [
            {"id": self.ASG1, "name": "Exam 1", "date_returned": "2026-08-20"},
            {"id": self.ASG2, "name": "Quiz 2", "date_returned": "2026-08-20"},
        ]

    def _build(self):
        from app.archive_export import build_class_archive_zip
        from app.models import Course, Homework
        from flask import session
        from app.routes import class_learning_objectives_summary

        class _Links:
            def __init__(self, rows):
                self.rows = rows
                self.start = 0
                self.end = None

            def select(self, *args, **kwargs):
                return self

            def eq(self, *args, **kwargs):
                return self

            def order(self, *args, **kwargs):
                return self

            def range(self, start, end):
                self.start = start
                self.end = end
                return self

            def execute(self):
                end = len(self.rows) - 1 if self.end is None else self.end
                return unittest.mock.MagicMock(data=self.rows[self.start:end + 1])

        class _Admin:
            def table(self, name):
                if name != "assignment_objectives":
                    raise AssertionError(name)
                return _Links(self_test._links())

        self_test = self
        hw = {self.S1: 88}
        prev = {self.S1: 70}

        def letters(assignment_id, students, learning_objectives):
            if assignment_id == self.ASG1:
                return {(self.S1, "lo-1"): "M"}
            return {}

        self._clear_attached_cache()
        try:
            with unittest.mock.patch.object(
                Course, "get_full_class_data", return_value=self._class_data()
            ) as loaded, \
                    unittest.mock.patch(
                        "app.archive_export._load_assignments",
                        return_value=self._assignments(),
                    ), \
                    unittest.mock.patch.object(
                        Course, "get_assignment_lo_ids", return_value=["lo-1"]
                    ), \
                    unittest.mock.patch.object(
                        Homework, "get_hw_scores_map_for_assignment", return_value=hw
                    ), \
                    unittest.mock.patch.object(
                        Homework, "get_hw_prev_scores_map_for_assignment", return_value=prev
                    ), \
                    unittest.mock.patch(
                        "app.routes._gradesheet_export_bundle"
                    ) as bundle, \
                    unittest.mock.patch(
                        "app.archive_export._gradesheet_letter_map_for_assignment",
                        side_effect=letters,
                    ), \
                    unittest.mock.patch("app.routes.supabase_admin", _Admin()):
                zip_bytes, manifest = build_class_archive_zip("c-archive")
                self.assertEqual(loaded.call_count, 1)
                bundle.assert_not_called()
                app = create_app()
                with app.test_request_context("/class/c-archive/learning-objectives"):
                    session["user_id"] = "inst"
                    session["role"] = "instructor"
                    with unittest.mock.patch("app.routes._instructor_owns_class", return_value=True):
                        html = class_learning_objectives_summary("c-archive")
        finally:
            self._clear_attached_cache()
        return zip_bytes, manifest, hw, prev, html

    def test_zip_matches_the_filled_builder_and_keeps_every_enrollment(self):
        import csv
        import zipfile
        import fitz
        from app.routes import (
            _build_gradesheet_csv_text,
            _gradesheet_csv_data_rows,
            learning_objective_cell_text,
            learning_objective_progress_text,
        )
        from app.student_names import row_sort_key

        zip_bytes, manifest, hw, prev, html = self._build()
        self.assertEqual(manifest["objectives_entry"], "learning_objectives.csv")
        self.assertEqual(manifest["source_assignments"], 2)
        self.assertEqual(manifest["source_students"], 4)

        ordered = sorted(
            [
                {"id": self.S3, "full_name": "Doe/Jane, Ann"},
                {"id": self.S1, "full_name": "Lee, Ana"},
                {"id": self.S2, "full_name": "Lee, Ana"},
                {"id": self.S4, "full_name": "Muted, Max"},
            ],
            key=row_sort_key,
        )
        los = [{"id": "lo-1", "vendor_code": "D1"}]
        expected = {}
        for assignment, letters in (
            (self._assignments()[0], {(self.S1, "lo-1"): "M"}),
            (self._assignments()[1], {}),
        ):
            rows = _gradesheet_csv_data_rows(ordered, los, hw, letters, True, prev)
            expected[f"assignments/{assignment['name']}_gradesheet.csv"] = _build_gradesheet_csv_text(
                assignment["name"],
                assignment["date_returned"],
                ["D1"],
                rows,
                False,
                True,
            )

        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
        names = archive.namelist()
        self.assertTrue(all(".." not in name.split("/") for name in names))
        for name in names:
            slashes = 0 if name == "learning_objectives.csv" else 1
            self.assertEqual(name.count("/"), slashes, name)
        quiz_files = [name for name in names if name.startswith("assignments/")]
        self.assertEqual(sorted(quiz_files), [
            "assignments/Exam 1_gradesheet.csv",
            "assignments/Quiz 2_gradesheet.csv",
        ])
        self.assertEqual(manifest["assignment_entries"], quiz_files)
        for name in quiz_files:
            text = archive.read(name).decode("utf-8")
            self.assertEqual(text, expected[name])
            self.assertNotIn("hidden@example.edu", text)
            self.assertNotIn("secret@example.edu", text)
            self.assertNotIn("ZZZ", text)
        quiz2 = archive.read("assignments/Quiz 2_gradesheet.csv").decode("utf-8")
        parsed = list(csv.reader(io.StringIO(quiz2.lstrip("\ufeff"))))
        header = next(row for row in parsed if row and row[0] == "Student Name")
        self.assertEqual(header[:3], ["Student Name", "HW prev", "HW"])
        muted_row = next(row for row in parsed if row and row[0] == "Muted, Max")
        self.assertEqual(muted_row[3], "")

        objectives = archive.read("learning_objectives.csv").decode("utf-8")
        self.assertNotIn("ZZZ", objectives)
        self.assertNotIn("secret@example.edu", objectives)
        grid = list(csv.reader(io.StringIO(objectives.lstrip("\ufeff"))))
        self.assertEqual(grid[0], ["Student", "D1", "Progress"])
        by_progress = {}
        for row in grid[1:]:
            by_progress.setdefault(row[0], []).append(row)
        self.assertEqual(by_progress["Muted, Max"][0][1], learning_objective_cell_text(1, 2))
        self.assertEqual(by_progress["Muted, Max"][0][2], learning_objective_progress_text(0, 1))
        lee_cells = {(row[1], row[2]) for row in by_progress["Lee, Ana"]}
        self.assertEqual(lee_cells, {
            (learning_objective_cell_text(2, 2), learning_objective_progress_text(1, 1)),
            (learning_objective_cell_text(0, 2), learning_objective_progress_text(0, 1)),
        })

        pdf_names = [name for name in names if name.startswith("students/")]
        self.assertEqual(manifest["student_entries"], pdf_names)
        self.assertEqual(sorted(pdf_names), [
            "students/Doe_Jane, Ann_gradesheet.pdf",
            "students/Lee, Ana__aaaaaaaa_gradesheet.pdf",
            "students/Lee, Ana__bbbbbbbb_gradesheet.pdf",
            "students/Muted, Max_gradesheet.pdf",
        ])

        passed_line = ""
        for name in pdf_names:
            document = fitz.open(stream=archive.read(name), filetype="pdf")
            text = "".join(page.get_text() for page in document)
            document.close()
            self.assertNotIn("@", text)
            self.assertNotIn(self.S1, text)
            self.assertNotIn(self.S4, text)
            if "Lee, Ana" in text and "1 / 1" in text:
                passed_line = text
                self.assertIn("Passed", text)
                self.assertNotIn("ZZZ", text)
            if name == "students/Muted, Max_gradesheet.pdf":
                self.assertIn("Muted, Max", text)
                self.assertIn("Passed", text)
        self.assertIn("Passed", passed_line)
        self.assertIn("1 / 1", passed_line)
        self.assertIn(learning_objective_cell_text(2, 2), html)
        self.assertIn(learning_objective_cell_text(0, 2), html)
        self.assertIn(learning_objective_progress_text(1, 1), html)
        self.assertNotIn("ZZZ", html)
        self.assertNotIn("Muted, Max", html)

    def test_thirty_student_pdfs_open_with_name_progress_and_embedded_font(self):
        import fitz
        from app.archive_export import _student_pdf
        from app.routes import _process_enrollments

        objectives = [
            {"id": "lo-1", "name": "D1", "vendor_code": "D1", "required_ms": 2},
            {"id": "lo-2", "name": "D2", "vendor_code": "D2", "required_ms": 2},
        ]
        enrollments = []
        for index in range(30):
            grades = [
                {"learning_objective_id": "lo-1", "top_score": "M", "counts_for_mastery": True}
                for _count in range(index % 3)
            ]
            grades.append({"learning_objective_id": "lo-2", "top_score": "R", "counts_for_mastery": True})
            enrollments.append({
                "muted": index % 5 == 0,
                "profiles": {
                    "id": f"student-{index:02d}",
                    "full_name": f"Student{index:02d}, Pat",
                    "email": f"s{index}@example.edu",
                    "grades": grades,
                },
            })
        students = _process_enrollments({"learning_objectives": objectives, "enrollments": enrollments})[1]
        self.assertEqual(len(students), 30)
        for student in students:
            document = fitz.open(stream=_student_pdf(student), filetype="pdf")
            text = "".join(page.get_text() for page in document)
            fonts = [font for page in document for font in page.get_fonts()]
            document.close()
            passed = sum(1 for lo in student["learning_objectives"] if lo["is_passed"])
            self.assertIn(student["name"], text)
            self.assertIn("Passed", text)
            self.assertIn(f"{passed} / {student['objective_total']}", text)
            for lo in student["learning_objectives"]:
                self.assertIn(f"{lo['m_count']} / {lo['required_ms']} Ms", text)
            self.assertNotIn("@", text)
            self.assertTrue(fonts, student["name"])
            for font in fonts:
                self.assertIn("FiraGO", font[3])
                self.assertNotEqual(font[1], "n/a")

    def test_entry_stem_keeps_plain_names_and_cleans_illegal_characters(self):
        from app.archive_export import archive_entry_stem
        self.assertEqual(archive_entry_stem("Exam 1", "assignment"), "Exam 1")
        self.assertEqual(archive_entry_stem("Estes, Brody", "student"), "Estes, Brody")
        self.assertEqual(archive_entry_stem("Exam 1: A/B?", "assignment"), "Exam 1_ A_B_")
        self.assertEqual(archive_entry_stem("A\nb", "student"), "A_b")
        self.assertEqual(archive_entry_stem("CON", "student"), "CON_")
        self.assertEqual(archive_entry_stem("..", "assignment"), "assignment")

    def test_duplicate_stems_get_the_id_suffix_ignoring_case(self):
        from app.archive_export import _suffix_duplicate_stems
        self.assertEqual(
            _suffix_duplicate_stems([
                ("Exam 1", self.ASG1),
                ("exam 1", self.ASG2),
                ("Quiz 3", "33333333-cccc-cccc-cccc-cccccccccccc"),
            ]),
            ["Exam 1__11111111", "exam 1__22222222", "Quiz 3"],
        )
        self.assertEqual(
            _suffix_duplicate_stems([("Lee, Ana", self.S1), ("Lee, Ana", self.S2)]),
            ["Lee, Ana__aaaaaaaa", "Lee, Ana__bbbbbbbb"],
        )

    def test_an_entry_name_cannot_be_written_twice(self):
        import zipfile
        from app.archive_export import ArchiveManifestError, _write_entry
        used = set()
        with zipfile.ZipFile(io.BytesIO(), "w") as archive:
            _write_entry(archive, "students/Lee, Ana_gradesheet.pdf", b"a", used)
            with self.assertRaises(ArchiveManifestError):
                _write_entry(archive, "students/LEE, ANA_gradesheet.pdf", b"b", used)

    def test_manifest_check_catches_a_missing_file_and_a_count_mismatch(self):
        import zipfile
        from app.archive_export import ArchiveManifestError, check_archive_manifest
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("assignments/Exam 1_gradesheet.csv", "a")
            archive.writestr("students/Estes, Brody_gradesheet.pdf", "b")
            archive.writestr("learning_objectives.csv", "c")
        zip_bytes = buffer.getvalue()
        manifest = {
            "source_assignments": 1,
            "source_students": 2,
            "assignment_entries": ["assignments/Exam 1_gradesheet.csv"],
            "student_entries": [
                "students/Estes, Brody_gradesheet.pdf",
                "students/Lee, Ana_gradesheet.pdf",
            ],
            "objectives_entry": "learning_objectives.csv",
        }
        with self.assertRaisesRegex(ArchiveManifestError, "Lee, Ana_gradesheet.pdf"):
            check_archive_manifest(manifest, zip_bytes)
        complete = dict(
            manifest,
            source_students=1,
            student_entries=["students/Estes, Brody_gradesheet.pdf"],
        )
        check_archive_manifest(complete, zip_bytes)
        with self.assertRaisesRegex(ArchiveManifestError, "student PDFs"):
            check_archive_manifest(dict(complete, source_students=2), zip_bytes)

    def _client(self):
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        return client

    def test_non_owner_gets_no_zip(self):
        client = self._client()
        with unittest.mock.patch("app.routes._instructor_owns_class", return_value=False), \
                unittest.mock.patch("app.archive_export.build_class_archive_zip") as build:
            response = client.get("/class/c-archive/export-archive")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/instructor/dashboard", response.headers["Location"])
        self.assertFalse(response.data.startswith(b"PK"))
        build.assert_not_called()

    def test_owner_download_names_the_zip_and_deletes_nothing(self):
        from werkzeug.utils import secure_filename
        client = self._client()
        manifest = {"class_name": "Alg II"}
        with unittest.mock.patch("app.routes._instructor_owns_class", return_value=True), \
                unittest.mock.patch(
                    "app.archive_export.build_class_archive_zip",
                    return_value=(b"PK\x03\x04archive", manifest),
                ), \
                unittest.mock.patch("app.routes.supabase_admin") as admin:
            admin.table.side_effect = AssertionError("export must not touch the database")
            response = client.get("/class/c-archive/export-archive")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"PK\x03\x04archive")
        filename = f"{secure_filename('Alg II') or 'class'}_archive.zip"
        self.assertIn(f'filename="{filename}"', response.headers["Content-Disposition"])

        empty = dict(manifest)
        empty["class_name"] = ""
        with unittest.mock.patch("app.routes._instructor_owns_class", return_value=True), \
                unittest.mock.patch(
                    "app.archive_export.build_class_archive_zip",
                    return_value=(b"PK\x03\x04archive", empty),
                ):
            fallback = client.get("/class/c-archive/export-archive")
        self.assertIn('filename="class_archive.zip"', fallback.headers["Content-Disposition"])


class _ArchiveBucket:
    def __init__(self, fail=None, download_bytes=None, missing_after=None, remove_error=None):
        self.calls = []
        self.events = []
        self.fail = fail
        self.payload = None
        self.download_bytes = download_bytes
        self.built = 0
        self.missing_after = missing_after
        self.remove_error = remove_error
        self.remove_count = 0

    def upload(self, path, data, options):
        self.events.append("upload")
        self.calls.append(("upload", path, data, options))
        if self.fail == "upload":
            raise RuntimeError("upload failed")
        self.payload = data

    def download(self, path):
        self.events.append("download")
        self.calls.append(("download", path))
        if self.fail == "download":
            raise RuntimeError("download failed")
        if self.download_bytes is not None:
            return self.download_bytes
        return self.payload

    def remove(self, paths):
        self.events.append("remove")
        self.calls.append(("remove", list(paths)))
        self.remove_count += 1
        if self.remove_error is not None:
            raise self.remove_error
        if self.fail == "remove":
            raise RuntimeError("remove failed")
        if self.missing_after is not None and self.remove_count > self.missing_after:
            from storage3.exceptions import StorageApiError
            raise StorageApiError("Object not found", "not_found", 404)


class TestClassArchiveRoute(unittest.TestCase):
    ARCHIVE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    CLASS_ROW = {
        "id": "c1",
        "name": "Archive Class",
        "semester": "Fall 2026",
        "days": "MW",
        "section_number": "01",
        "instructor_id": "inst1",
    }
    KEPT = (
        "The class was archived, but it was not fully removed. "
        "The archive is saved under Archived Classes. "
        "Delete the class to finish removing it."
    )
    CHANGED = (
        "The archive took too long, or the class changed. Nothing was removed. Try again."
    )

    def _zip(self, students=1, assignments=1, body=b"csv"):
        import zipfile
        student_entries = [f"students/S{index}_gradesheet.pdf" for index in range(students)]
        assignment_entries = [f"assignments/A{index}_gradesheet.csv" for index in range(assignments)]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name in assignment_entries:
                archive.writestr(name, body)
            for name in student_entries:
                archive.writestr(name, b"pdf")
            archive.writestr("learning_objectives.csv", "lo")
        manifest = {
            "assignment_entries": assignment_entries,
            "student_entries": student_entries,
            "objectives_entry": "learning_objectives.csv",
            "source_assignments": assignments,
            "source_students": students,
            "class_name": "Archive Class",
        }
        return buffer.getvalue(), manifest

    def _post(
        self,
        data=None,
        send_csrf=True,
        role="instructor",
        class_rows=None,
        fail_insert=None,
        bucket_fail=None,
        download_bytes=None,
        built=None,
        build_error=None,
        vanish_on=None,
        monotonic=None,
        delete_error=False,
        owned=None,
        include_confirm=True,
    ):
        from uuid import UUID
        from app import routes as r
        import app.archive_storage as storage

        log = []
        deleted = []
        selects = {"n": 0}
        bucket = _ArchiveBucket(fail=bucket_fail, download_bytes=download_bytes)
        if class_rows is None:
            class_rows = [dict(self.CLASS_ROW)]
        rows_for = {"classes": class_rows}

        def table(name):
            query = _RemovalQuery(log, name, rows_for.get(name, []), None, fail_insert=fail_insert)
            real_execute = query.execute

            def execute():
                if name == "classes" and query.op == "select" and vanish_on is not None:
                    selects["n"] += 1
                    if selects["n"] >= vanish_on:
                        query.rows = []
                result = real_execute()
                if name == "archived_classes" and query.op == "insert":
                    bucket.events.append("insert")
                return result

            query.execute = execute
            return query

        admin = unittest.mock.MagicMock()
        admin.table.side_effect = table
        admin.storage.from_.return_value = bucket
        zip_bytes, manifest = self._zip()
        if built is not None:
            zip_bytes, manifest = built

        def build_zip(_class_id):
            bucket.built += 1
            if build_error is not None:
                raise build_error
            return zip_bytes, manifest

        def delete_rows(*_args):
            bucket.events.append("delete_class_rows")
            deleted.append("delete_class_rows")
            if delete_error:
                raise RuntimeError("delete failed")

        client = create_app().test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = role
            sess["csrf_token"] = "test-csrf"
        if data is None and include_confirm:
            data = {"confirm_name": "Archive Class"}
        elif data is None:
            data = {}
        headers = {"X-CSRF-Token": "test-csrf"} if send_csrf else {}
        patches = [
            unittest.mock.patch.object(r, "supabase_admin", admin),
            unittest.mock.patch.object(storage, "supabase_admin", admin),
            unittest.mock.patch("app.archive_export.build_class_archive_zip", side_effect=build_zip),
            unittest.mock.patch.object(r, "uuid4", return_value=UUID(self.ARCHIVE_ID)),
            unittest.mock.patch.object(r, "delete_class_rows", side_effect=delete_rows),
        ]
        if owned is not None:
            patches.append(unittest.mock.patch.object(r, "_owned_class_row", side_effect=owned))
        if monotonic is not None:
            patches.append(unittest.mock.patch.object(r.time, "monotonic", side_effect=monotonic))
        for item in patches:
            item.start()
        try:
            response = client.post("/class/c1/archive", data=data, headers=headers)
        finally:
            for item in patches:
                item.stop()
        return response, log, bucket, deleted

    def test_non_instructor_and_missing_class_do_not_touch_storage(self):
        denied, denied_log, denied_bucket, _deleted = self._post(role="student")
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied_bucket.calls, [])
        self.assertEqual(denied_log, [])
        missing, log, bucket, deleted = self._post(class_rows=[])
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(
            missing.get_json()["error"],
            "This class was not found. It may already be archived.",
        )
        self.assertEqual(bucket.calls, [])
        self.assertEqual(deleted, [])
        selects = [entry for entry in log if entry["op"] == "select"]
        self.assertEqual(len(selects), 1)
        self.assertIn(("eq", "id", "c1"), selects[0]["filters"])
        self.assertIn(("eq", "instructor_id", "inst1"), selects[0]["filters"])

    def test_wrong_typed_name_is_400_and_does_not_build(self):
        for typed in ("archive class", "Archive Class "):
            response, _log, bucket, deleted = self._post(data={"confirm_name": typed})
            self.assertEqual(response.status_code, 400, typed)
            self.assertEqual(
                response.get_json()["error"],
                "Type the class name exactly to archive it.",
            )
            self.assertEqual(bucket.built, 0)
            self.assertEqual(bucket.calls, [])
            self.assertEqual(deleted, [])

    def test_missing_confirm_name_is_the_same_400(self):
        response, _log, bucket, _deleted = self._post(include_confirm=False)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["success"], False)
        self.assertEqual(
            response.get_json()["error"],
            "Type the class name exactly to archive it.",
        )
        self.assertEqual(bucket.built, 0)
        self.assertEqual(bucket.calls, [])

    def test_missing_csrf_token_is_403(self):
        response, _log, bucket, _deleted = self._post(send_csrf=False)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "Invalid CSRF token")
        self.assertEqual(bucket.calls, [])

    def test_build_or_manifest_failure_does_not_upload(self):
        failed, _log, bucket, _deleted = self._post(build_error=RuntimeError("build failed"))
        self.assertEqual(failed.status_code, 500)
        self.assertEqual(
            failed.get_json()["error"],
            "Could not build the archive. Nothing was changed.",
        )
        self.assertEqual(bucket.calls, [])
        bad_zip, manifest = self._zip()
        manifest = dict(manifest)
        manifest["source_students"] = 9
        mismatched, _log, mismatch_bucket, _deleted = self._post(built=(bad_zip, manifest))
        self.assertEqual(mismatched.status_code, 500)
        self.assertEqual(
            mismatched.get_json()["error"],
            "Could not build the archive. Nothing was changed.",
        )
        self.assertEqual(mismatch_bucket.calls, [])

    def test_oversized_zip_is_413_and_does_not_upload(self):
        import zipfile
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("assignments/A0_gradesheet.csv", b"x" * 51_000_000)
            archive.writestr("students/S0_gradesheet.pdf", b"pdf")
            archive.writestr("learning_objectives.csv", "lo")
        payload = buffer.getvalue()
        self.assertGreater(len(payload), 50_000_000)
        manifest = {
            "assignment_entries": ["assignments/A0_gradesheet.csv"],
            "student_entries": ["students/S0_gradesheet.pdf"],
            "objectives_entry": "learning_objectives.csv",
            "source_assignments": 1,
            "source_students": 1,
        }
        response, _log, bucket, deleted = self._post(built=(payload, manifest))
        self.assertEqual(response.status_code, 413)
        megabytes = f"{len(payload) / 1_000_000:.1f}"
        self.assertEqual(
            response.get_json()["error"],
            f"This archive is {megabytes} MB, over the 50 MB limit. Nothing was changed.",
        )
        self.assertEqual(bucket.calls, [])
        self.assertEqual(deleted, [])

    def test_upload_error_removes_and_does_not_insert_or_delete(self):
        response, log, bucket, deleted = self._post(bucket_fail="upload")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.get_json()["error"],
            "Could not save the archive. The class was not changed.",
        )
        self.assertEqual(bucket.calls[-1][0], "remove")
        self.assertEqual(bucket.calls[-1][1], [f"inst1/{self.ARCHIVE_ID}.zip"])
        self.assertFalse(any(entry["table"] == "archived_classes" for entry in log))
        self.assertEqual(deleted, [])

    def test_download_mismatch_or_bad_manifest_removes_without_insert(self):
        other, _manifest = self._zip(body=b"other")
        empty = io.BytesIO()
        import zipfile
        with zipfile.ZipFile(empty, "w") as archive:
            archive.writestr("learning_objectives.csv", "lo")
        cases = (
            {"bucket_fail": "download"},
            {"download_bytes": other},
            {"download_bytes": empty.getvalue()},
        )
        for case in cases:
            response, log, bucket, deleted = self._post(**case)
            self.assertEqual(response.status_code, 500)
            self.assertEqual(
                response.get_json()["error"],
                "The saved archive did not verify and was removed. The class was not changed.",
            )
            self.assertEqual(bucket.calls[-1][0], "remove")
            self.assertFalse(any(entry["op"] == "insert" for entry in log))
            self.assertEqual(deleted, [])

    def test_missing_class_or_spent_budget_removes_without_insert(self):
        missing, missing_log, missing_bucket, _deleted = self._post(vanish_on=2)
        self.assertEqual(missing.status_code, 500)
        self.assertEqual(missing.get_json()["error"], self.CHANGED)
        self.assertEqual(missing_bucket.calls[-1][0], "remove")
        self.assertFalse(any(entry["op"] == "insert" for entry in missing_log))
        late, late_log, late_bucket, _deleted = self._post(monotonic=[0, 201])
        self.assertEqual(late.status_code, 500)
        self.assertEqual(late.get_json()["error"], self.CHANGED)
        self.assertEqual(late_bucket.calls[-1][0], "remove")
        self.assertFalse(any(entry["op"] == "insert" for entry in late_log))

    def test_second_ownership_check_error_removes_without_insert(self):
        calls = {"n": 0}

        def owned(_class_id, _instructor_id):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("lookup failed")
            return dict(self.CLASS_ROW)

        response, log, bucket, deleted = self._post(owned=owned)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["error"], self.CHANGED)
        self.assertEqual([call[0] for call in bucket.calls], ["upload", "download", "remove"])
        self.assertFalse(any(entry["op"] == "insert" for entry in log))
        self.assertEqual(deleted, [])

    def test_insert_error_deletes_the_row_then_removes_the_object(self):
        response, log, bucket, deleted = self._post(fail_insert="archived_classes")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.get_json()["error"],
            "Could not record the archive. The class was not changed.",
        )
        self.assertEqual(
            [entry["op"] for entry in log if entry["table"] == "archived_classes"],
            ["insert-failed", "delete"],
        )
        deleted_row = next(
            entry for entry in log
            if entry["table"] == "archived_classes" and entry["op"] == "delete"
        )
        self.assertIn(("eq", "id", self.ARCHIVE_ID), deleted_row["filters"])
        self.assertEqual(bucket.calls[-1][0], "remove")
        self.assertEqual(deleted, [])

    def test_missing_class_after_insert_keeps_the_archive(self):
        response, log, bucket, deleted = self._post(vanish_on=3)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["redirect"].endswith("/instructor/dashboard"))
        self.assertTrue(any(
            entry["table"] == "archived_classes" and entry["op"] == "insert" for entry in log
        ))
        self.assertFalse(any(call[0] == "remove" for call in bucket.calls))
        self.assertEqual(deleted, [])

    def test_third_ownership_check_error_keeps_the_archive(self):
        calls = {"n": 0}

        def owned(_class_id, _instructor_id):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("lookup failed")
            return dict(self.CLASS_ROW)

        response, log, bucket, deleted = self._post(owned=owned)
        self.assertEqual(response.status_code, 500)
        body = response.get_json()
        self.assertEqual(body["success"], False)
        self.assertEqual(body["archived"], True)
        self.assertEqual(body["error"], self.KEPT)
        self.assertTrue(any(
            entry["table"] == "archived_classes" and entry["op"] == "insert" for entry in log
        ))
        self.assertFalse(any(call[0] == "remove" for call in bucket.calls))
        self.assertEqual(deleted, [])

    def test_delete_failure_keeps_the_row_and_object(self):
        response, log, bucket, deleted = self._post(delete_error=True)
        self.assertEqual(response.status_code, 500)
        body = response.get_json()
        self.assertEqual(body["success"], False)
        self.assertEqual(body["archived"], True)
        self.assertEqual(body["error"], self.KEPT)
        self.assertEqual(deleted, ["delete_class_rows"])
        self.assertTrue(any(
            entry["table"] == "archived_classes" and entry["op"] == "insert" for entry in log
        ))
        self.assertFalse(any(call[0] == "remove" for call in bucket.calls))

    def test_happy_path_uploads_records_and_deletes_in_order(self):
        zip_bytes, manifest = self._zip(students=2, assignments=1)
        response, log, bucket, deleted = self._post(built=(zip_bytes, manifest))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["success"], True)
        self.assertTrue(response.get_json()["redirect"].endswith("/instructor/dashboard"))
        self.assertEqual(
            bucket.events,
            ["upload", "download", "insert", "delete_class_rows"],
        )
        self.assertEqual([call[0] for call in bucket.calls], ["upload", "download"])
        path = f"inst1/{self.ARCHIVE_ID}.zip"
        self.assertEqual(bucket.calls[0][1], path)
        self.assertEqual(bucket.calls[0][2], zip_bytes)
        self.assertEqual(bucket.calls[0][3], {"content-type": "application/zip", "upsert": "false"})
        inserted = next(
            entry for entry in log
            if entry["table"] == "archived_classes" and entry["op"] == "insert"
        )
        self.assertEqual(inserted["rows"]["storage_path"], path)
        self.assertEqual(inserted["rows"]["student_count"], 2)
        self.assertEqual(inserted["rows"]["assignment_count"], 1)
        self.assertEqual(inserted["rows"]["class_name"], "Archive Class")
        self.assertEqual(inserted["rows"]["semester"], "Fall 2026")
        self.assertEqual(inserted["rows"]["days"], "MW")
        self.assertEqual(inserted["rows"]["section_number"], "01")
        self.assertEqual(inserted["rows"]["size_bytes"], len(zip_bytes))
        self.assertEqual(deleted, ["delete_class_rows"])

    def test_student_count_stores_the_enrolled_total_including_muted(self):
        _zip_bytes, manifest = self._zip(students=2)
        response, log, _bucket, _deleted = self._post(built=(_zip_bytes, manifest))
        self.assertEqual(response.status_code, 200)
        inserted = next(
            entry["rows"] for entry in log
            if entry["table"] == "archived_classes" and entry["op"] == "insert"
        )
        self.assertEqual(inserted["student_count"], manifest["source_students"])
        self.assertEqual(inserted["student_count"], 2)

    def test_each_upload_gets_its_own_options_dict(self):
        from app.archive_storage import upload_archive
        import app.archive_storage as storage
        bucket = _ArchiveBucket()
        admin = unittest.mock.MagicMock()
        admin.storage.from_.return_value = bucket
        with unittest.mock.patch.object(storage, "supabase_admin", admin):
            upload_archive("one.zip", b"a")
            upload_archive("two.zip", b"b")
        self.assertEqual(bucket.calls[0][3], {"content-type": "application/zip", "upsert": "false"})
        self.assertEqual(bucket.calls[0][3], bucket.calls[1][3])
        self.assertIsNot(bucket.calls[0][3], bucket.calls[1][3])
        self.assertEqual(bucket.calls[0][1], "one.zip")
        self.assertEqual(admin.storage.from_.call_args.args[0], "class-archives")


class TestArchivedClasses(unittest.TestCase):
    ARCHIVE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    OTHER_ID = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    MISSING_ID = "cccccccc-dddd-eeee-ffff-000000000000"

    def _row(self, archive_id, instructor_id, name="Algebra"):
        return {
            "id": archive_id,
            "instructor_id": instructor_id,
            "class_name": name,
            "semester": "Fall 2026",
            "days": "MW",
            "section_number": "01",
            "student_count": 2,
            "assignment_count": 1,
            "size_bytes": 1500,
            "archived_at": "2026-10-15T18:04:00+00:00",
            "storage_path": f"{instructor_id}/{archive_id}.zip",
        }

    def _request(
        self,
        method,
        path,
        rows,
        data=None,
        send_csrf=True,
        role="instructor",
        user_id="inst-a",
        bucket=None,
        fail_first_row_delete=False,
        shared=None,
    ):
        from app import routes as r
        import app.archive_storage as storage

        log = []
        seen = {"columns": "", "orders": []}
        shared = shared if shared is not None else {"row_deletes": 0}
        bucket = bucket or _ArchiveBucket(download_bytes=b"PK\x03\x04archive")

        def table(name):
            query = _RemovalQuery(log, name, rows, None)
            real_select = query.select
            real_order = query.order
            real_execute = query.execute

            def select(*args, **kwargs):
                if args:
                    seen["columns"] = args[0]
                return real_select(*args, **kwargs)

            def order(*args, **kwargs):
                seen["orders"].append((args, kwargs))
                return real_order(*args, **kwargs)

            def execute():
                if query.op == "select":
                    kept = []
                    for row in query.rows:
                        matched = True
                        for kind, column, value in query.filters:
                            if kind == "eq" and str(row.get(column)) != str(value):
                                matched = False
                        if matched:
                            kept.append(row)
                    query.rows = kept
                if (
                    fail_first_row_delete
                    and query.op == "delete"
                    and name == "archived_classes"
                    and shared["row_deletes"] == 0
                ):
                    shared["row_deletes"] += 1
                    raise RuntimeError("row delete failed")
                return real_execute()

            query.select = select
            query.order = order
            query.execute = execute
            return query

        admin = unittest.mock.MagicMock()
        admin.table.side_effect = table
        admin.storage.from_.return_value = bucket
        client = create_app().test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["role"] = role
            sess["csrf_token"] = "test-csrf"
        headers = {"X-CSRF-Token": "test-csrf"} if send_csrf and method != "GET" else {}
        with unittest.mock.patch.object(r, "supabase_admin", admin), \
                unittest.mock.patch.object(storage, "supabase_admin", admin):
            if method == "GET":
                response = client.get(path)
            else:
                response = client.post(path, data=data or {}, headers=headers)
        return response, log, bucket, seen

    def test_list_is_limited_to_the_signed_in_instructor_and_uses_archived_at(self):
        rows = [
            self._row(self.ARCHIVE_ID, "inst-a", "Algebra"),
            self._row(self.OTHER_ID, "inst-b", "Secret Class"),
        ]
        response, _log, bucket, seen = self._request("GET", "/archived-classes", rows)
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Algebra", page)
        self.assertNotIn("Secret Class", page)
        self.assertIn("archived_at", seen["columns"])
        self.assertNotIn("created_at", seen["columns"])
        self.assertEqual(seen["orders"][0][0], ("archived_at",))
        self.assertTrue(seen["orders"][0][1].get("desc"))
        self.assertEqual(seen["orders"][1][0], ("id",))
        self.assertEqual(bucket.calls, [])

    def test_another_instructor_or_a_missing_id_cannot_download_or_delete(self):
        rows = [self._row(self.ARCHIVE_ID, "inst-a"), self._row(self.OTHER_ID, "inst-b", "Secret Class")]
        download, _log, bucket, _seen = self._request(
            "GET", f"/archived-classes/{self.OTHER_ID}/download", rows
        )
        self.assertEqual(download.status_code, 404)
        self.assertEqual(download.get_json()["error"], "This archive was not found.")
        self.assertEqual(bucket.calls, [])
        deleted, delete_log, delete_bucket, _seen = self._request(
            "POST",
            f"/archived-classes/{self.OTHER_ID}/delete",
            rows,
            data={"confirm_name": "Secret Class"},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.get_json()["success"], True)
        self.assertTrue(deleted.get_json()["redirect"].endswith("/archived-classes"))
        self.assertEqual(delete_bucket.calls, [])
        self.assertFalse(any(entry["op"] == "delete" for entry in delete_log))
        missing, _log, missing_bucket, _seen = self._request(
            "GET", f"/archived-classes/{self.MISSING_ID}/download", rows
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.get_json()["error"], "This archive was not found.")
        self.assertEqual(missing_bucket.calls, [])

    def test_a_missing_row_delete_returns_success_with_a_redirect(self):
        first, _log, bucket, _seen = self._request(
            "POST",
            f"/archived-classes/{self.MISSING_ID}/delete",
            [],
            data={"confirm_name": "Algebra"},
        )
        second, second_log, second_bucket, _seen = self._request(
            "POST",
            f"/archived-classes/{self.MISSING_ID}/delete",
            [],
            data={"confirm_name": "Algebra"},
        )
        for response in (first, second):
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["success"], True)
            self.assertTrue(response.get_json()["redirect"].endswith("/archived-classes"))
        self.assertEqual(bucket.calls, [])
        self.assertEqual(second_bucket.calls, [])
        self.assertFalse(any(entry["op"] == "delete" for entry in second_log))

    def test_an_invalid_uuid_is_404(self):
        response, log, bucket, _seen = self._request(
            "GET", "/archived-classes/not-a-uuid/download", []
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "This archive was not found.")
        deleted, _log, _bucket, _seen = self._request(
            "POST", "/archived-classes/not-a-uuid/delete", [], data={"confirm_name": "Algebra"}
        )
        self.assertEqual(deleted.status_code, 404)
        self.assertEqual(bucket.calls, [])
        self.assertEqual(log, [])

    def test_a_trailing_newline_is_not_a_valid_download_id(self):
        response, log, bucket, _seen = self._request(
            "GET",
            f"/archived-classes/{self.ARCHIVE_ID}%0A/download",
            [self._row(self.ARCHIVE_ID, "inst-a")],
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "This archive was not found.")
        self.assertEqual(log, [])
        self.assertEqual(bucket.calls, [])

    def test_download_returns_the_zip_without_a_csrf_token(self):
        rows = [self._row(self.ARCHIVE_ID, "inst-a", "Algebra")]
        response, _log, _bucket, _seen = self._request(
            "GET", f"/archived-classes/{self.ARCHIVE_ID}/download", rows, send_csrf=False
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"PK\x03\x04archive")
        self.assertEqual(response.mimetype, "application/zip")
        self.assertIn('filename="Algebra_archive.zip"', response.headers["Content-Disposition"])

    def test_wrong_typed_name_removes_nothing(self):
        rows = [self._row(self.ARCHIVE_ID, "inst-a", "Algebra")]
        response, log, bucket, _seen = self._request(
            "POST",
            f"/archived-classes/{self.ARCHIVE_ID}/delete",
            rows,
            data={"confirm_name": "algebra"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"],
            "Type the class name exactly to delete this archive.",
        )
        self.assertEqual(bucket.calls, [])
        self.assertFalse(any(entry["op"] == "delete" for entry in log))

    def test_delete_removes_the_object_before_the_row(self):
        rows = [self._row(self.ARCHIVE_ID, "inst-a", "Algebra")]
        response, log, bucket, _seen = self._request(
            "POST",
            f"/archived-classes/{self.ARCHIVE_ID}/delete",
            rows,
            data={"confirm_name": "Algebra"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(bucket.events[0], "remove")
        self.assertEqual(bucket.calls[0][1], [f"inst-a/{self.ARCHIVE_ID}.zip"])
        deleted = [entry for entry in log if entry["op"] == "delete"]
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0]["table"], "archived_classes")
        self.assertIn(("eq", "id", self.ARCHIVE_ID), deleted[0]["filters"])
        self.assertIn(("eq", "instructor_id", "inst-a"), deleted[0]["filters"])

    def test_a_missing_storage_object_still_deletes_the_row(self):
        from storage3.exceptions import StorageApiError
        rows = [self._row(self.ARCHIVE_ID, "inst-a", "Algebra")]
        response, log, bucket, _seen = self._request(
            "POST",
            f"/archived-classes/{self.ARCHIVE_ID}/delete",
            rows,
            data={"confirm_name": "Algebra"},
            bucket=_ArchiveBucket(remove_error=StorageApiError("Object not found", "not_found", 404)),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["success"], True)
        self.assertTrue(response.get_json()["redirect"].endswith("/archived-classes"))
        self.assertEqual(bucket.events, ["remove"])
        self.assertTrue(any(
            entry["op"] == "delete" and entry["table"] == "archived_classes" for entry in log
        ))

    def test_a_storage_500_keeps_the_row(self):
        from storage3.exceptions import StorageApiError
        rows = [self._row(self.ARCHIVE_ID, "inst-a", "Algebra")]
        response, log, bucket, _seen = self._request(
            "POST",
            f"/archived-classes/{self.ARCHIVE_ID}/delete",
            rows,
            data={"confirm_name": "Algebra"},
            bucket=_ArchiveBucket(remove_error=StorageApiError("storage failed", "404", 500)),
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.get_json()["error"],
            "Could not delete the archive file. The record was kept.",
        )
        self.assertFalse(any(entry["op"] == "delete" for entry in log))
        self.assertEqual(bucket.events, ["remove"])

    def test_a_network_error_keeps_the_row(self):
        rows = [self._row(self.ARCHIVE_ID, "inst-a", "Algebra")]
        response, log, bucket, _seen = self._request(
            "POST",
            f"/archived-classes/{self.ARCHIVE_ID}/delete",
            rows,
            data={"confirm_name": "Algebra"},
            bucket=_ArchiveBucket(remove_error=ConnectionError("network down")),
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.get_json()["error"],
            "Could not delete the archive file. The record was kept.",
        )
        self.assertFalse(any(entry["op"] == "delete" for entry in log))
        self.assertEqual(bucket.events, ["remove"])

    def test_a_failed_file_removal_keeps_the_row(self):
        rows = [self._row(self.ARCHIVE_ID, "inst-a", "Algebra")]
        response, log, bucket, _seen = self._request(
            "POST",
            f"/archived-classes/{self.ARCHIVE_ID}/delete",
            rows,
            data={"confirm_name": "Algebra"},
            bucket=_ArchiveBucket(fail="remove"),
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.get_json()["error"],
            "Could not delete the archive file. The record was kept.",
        )
        self.assertFalse(any(entry["op"] == "delete" for entry in log))
        self.assertEqual(bucket.events, ["remove"])

    def test_a_failed_row_delete_can_be_retried_after_the_file_is_gone(self):
        rows = [self._row(self.ARCHIVE_ID, "inst-a", "Algebra")]
        shared = {"row_deletes": 0}
        bucket = _ArchiveBucket(missing_after=1)
        first, first_log, _bucket, _seen = self._request(
            "POST",
            f"/archived-classes/{self.ARCHIVE_ID}/delete",
            rows,
            data={"confirm_name": "Algebra"},
            bucket=bucket,
            fail_first_row_delete=True,
            shared=shared,
        )
        self.assertEqual(first.status_code, 500)
        self.assertEqual(
            first.get_json()["error"],
            "The file was deleted but the record could not be removed. Try again.",
        )
        self.assertFalse(any(entry["op"] == "delete" for entry in first_log))
        second, second_log, _bucket, _seen = self._request(
            "POST",
            f"/archived-classes/{self.ARCHIVE_ID}/delete",
            rows,
            data={"confirm_name": "Algebra"},
            bucket=bucket,
            fail_first_row_delete=True,
            shared=shared,
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()["success"], True)
        self.assertTrue(second.get_json()["redirect"].endswith("/archived-classes"))
        self.assertEqual(bucket.remove_count, 2)
        self.assertTrue(any(
            entry["op"] == "delete" and entry["table"] == "archived_classes" for entry in second_log
        ))

    def test_delete_without_csrf_does_nothing(self):
        rows = [self._row(self.ARCHIVE_ID, "inst-a", "Algebra")]
        response, log, bucket, _seen = self._request(
            "POST",
            f"/archived-classes/{self.ARCHIVE_ID}/delete",
            rows,
            data={"confirm_name": "Algebra"},
            send_csrf=False,
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "Invalid CSRF token")
        self.assertEqual(bucket.calls, [])
        self.assertEqual(log, [])

    def test_empty_state_renders(self):
        response, _log, _bucket, _seen = self._request("GET", "/archived-classes", [])
        self.assertEqual(response.status_code, 200)
        self.assertIn("No archived classes yet.", response.get_data(as_text=True))

    def test_the_selection_page_has_the_blue_button(self):
        from flask import render_template, session
        from app.routes import CLASS_DAYS
        app = create_app()
        with app.test_request_context("/instructor/dashboard"):
            session["role"] = "instructor"
            html = render_template(
                "instructor_select_class.html",
                classes=[],
                create_class_token="t",
                copy_class_token="c",
                class_days=CLASS_DAYS,
            )
        self.assertIn('id="archivedClassesLink"', html)
        self.assertIn("bg-blue-600", html)
        self.assertIn("Archived Classes", html)
        self.assertIn("/archived-classes", html)
        self.assertIn("archivedClassesLink", html)

    def test_archived_page_escapes_the_class_name_in_the_data_attribute(self):
        rows = [self._row(self.ARCHIVE_ID, "inst-a", 'Algebra "A" <b>')]
        response, _log, _bucket, _seen = self._request("GET", "/archived-classes", rows)
        page = response.get_data(as_text=True)
        buttons = [
            line.strip()
            for line in page.splitlines()
            if "<button" in line and "delete-archive-btn" in line
        ]
        self.assertEqual(len(buttons), 1)
        escaped = 'data-class-name="Algebra &#34;A&#34; &lt;b&gt;"'
        self.assertIn(escaped, buttons[0])
        self.assertNotIn("<b>", buttons[0])
        self.assertNotIn('data-class-name="Algebra "', buttons[0])
        self.assertIn(f'data-archive-id="{self.ARCHIVE_ID}"', buttons[0])
        self.assertIn('datetime="2026-10-15T18:04:00+00:00"', page)
        self.assertIn("This permanently deletes the saved zip. It cannot be undone.", page)


class TestSignupEmailRedirect(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.payload = {
            "name": "Ada Lovelace",
            "email": "ada@example.com",
            "password": "correct-horse",
            "invite_code": "",
        }

    def _redirect(self, *, base_url, public_base=None, headers=None):
        from app.authentication import supabase
        supabase.auth.sign_up.return_value = unittest.mock.MagicMock(user=None)
        supabase.auth.sign_up.reset_mock()
        env = {"PUBLIC_BASE_URL": "" if public_base is None else public_base}
        with unittest.mock.patch("config.Config.SIGNUP_INVITE_CODE", ""), \
                unittest.mock.patch.dict(os.environ, env, clear=False):
            self.client.post(
                "/api/signup",
                json=self.payload,
                base_url=base_url,
                headers=headers or {},
            )
        kwargs = supabase.auth.sign_up.call_args.args[0]
        return kwargs["options"]["email_redirect_to"]

    def test_https_proxy_host_when_public_base_unset(self):
        url = self._redirect(
            base_url="http://grader.example.com",
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(url, "https://grader.example.com/login")

    def test_public_base_url_wins(self):
        url = self._redirect(
            base_url="http://grader.example.com",
            public_base="https://clarity.example/",
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(url, "https://clarity.example/login")

    def test_localhost_uses_request_host(self):
        url = self._redirect(base_url="http://localhost:5000")
        self.assertEqual(url, "http://localhost:5000/login")


class TestCsvUploadMarkup(unittest.TestCase):
    def _read(self, name):
        path = os.path.join(os.path.dirname(__file__), "..", "app", "templates", name)
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_upload_and_assignments_have_csv_controls(self):
        upload = self._read("update_grade.html")
        assignments = self._read("class_assignments.html")
        self.assertIn('id="csvInput"', upload)
        self.assertIn('accept=".csv"', upload)
        self.assertIn('id="loCsvInput"', assignments)
        self.assertIn('id="loCsvChooseBtn"', assignments)
        self.assertIn('id="loCsvTemplateCount"', assignments)
        self.assertIn("downloadLoCsvTemplate()", assignments)


class TestLoCsvPreviewModalScrollLock(unittest.TestCase):
    def _read(self, *parts):
        path = os.path.join(os.path.dirname(__file__), "..", *parts)
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _opening_tag(self, html, element_id):
        match = re.search(
            r'<div\s+id="' + re.escape(element_id) + r'"[^>]*>',
            html,
        )
        self.assertIsNotNone(match, element_id)
        return match.group(0)

    def test_lo_preview_overlay_has_no_onclick(self):
        src = self._read("app", "templates", "class_assignments.html")
        tag = self._opening_tag(src, "loCsvPreviewModal")
        self.assertNotIn("onclick", tag.lower())

    def test_assignments_calls_shared_scroll_helpers(self):
        src = self._read("app", "templates", "class_assignments.html")
        self.assertIn("ClarityUI.lockPageScroll()", src)
        self.assertIn("ClarityUI.unlockPageScroll()", src)

    def test_ui_helpers_defines_and_exports_scroll_helpers(self):
        src = self._read("app", "static", "js", "ui_helpers.js")
        self.assertIn("function lockPageScroll()", src)
        self.assertIn("function unlockPageScroll()", src)
        self.assertIn("lockPageScroll,", src)
        self.assertIn("unlockPageScroll,", src)

    def test_student_upload_overlays_have_no_onclick(self):
        src = self._read("app", "templates", "class_detail.html")
        for element_id in ("uploadStudentsModal", "studentUploadPreviewModal"):
            tag = self._opening_tag(src, element_id)
            self.assertNotIn("onclick", tag.lower())
        self.assertIn("ClarityUI.lockPageScroll()", src)
        self.assertIn("ClarityUI.unlockPageScroll()", src)
        self.assertNotIn("setPageScrollLocked", src)

    def test_class_detail_lo_csv_preview_code_is_gone(self):
        src = self._read("app", "templates", "class_detail.html")
        for token in (
            "loCsvPreviewModal",
            "loCsvConfig",
            "downloadLoCsvTemplate",
            "closeLoCsvPreviewModal",
            "LO_CLASS_ID",
        ):
            self.assertNotIn(token, src)

    def test_both_pages_load_ui_helpers(self):
        for name in ("class_assignments.html", "class_detail.html"):
            src = self._read("app", "templates", name)
            self.assertIn('{% extends "app_base.html" %}', src)
        base = self._read("app", "templates", "app_base.html")
        self.assertIn("js/ui_helpers.js", base)

    def test_scroll_lock_count_changes_overflow_only_on_transitions(self):
        src = self._read("app", "static", "js", "ui_helpers.js")
        lock = src.split("function lockPageScroll()", 1)[1].split("function unlockPageScroll()", 1)[0]
        unlock = src.split("function unlockPageScroll()", 1)[1].split("window.ClarityUI", 1)[0]
        self.assertIn("pageScrollLockCount += 1", lock)
        gate = lock.find("if (pageScrollLockCount === 1)")
        add_at = lock.find('classList.add("overflow-hidden")')
        self.assertGreater(gate, -1)
        self.assertGreater(add_at, gate)
        self.assertEqual(lock.count('classList.add("overflow-hidden")'), 2)
        self.assertNotIn('classList.remove("overflow-hidden")', lock)
        self.assertIn("if (pageScrollLockCount === 0) return", unlock)
        dec = unlock.find("pageScrollLockCount -= 1")
        gate0 = unlock.find("if (pageScrollLockCount === 0)", dec)
        remove_at = unlock.find('classList.remove("overflow-hidden")')
        self.assertGreater(dec, -1)
        self.assertGreater(gate0, dec)
        self.assertGreater(remove_at, gate0)
        self.assertEqual(unlock.count('classList.remove("overflow-hidden")'), 2)
        self.assertNotIn('classList.add("overflow-hidden")', unlock)


class TestGeminiDependencyGone(unittest.TestCase):
    def test_requirements_and_app_have_no_genai(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        req_path = os.path.join(root, "requirements.txt")
        with open(req_path, encoding="utf-8") as f:
            requirements = f.read()
        self.assertNotIn("google-genai", requirements.lower())
        app_dir = os.path.join(root, "app")
        for dirpath, _, files in os.walk(app_dir):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                self.assertNotIn("google.genai", text)
                self.assertNotIn("GEMINI_", text)


class TestPrintClassSheet(unittest.TestCase):
    def _template(self, name):
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "templates", name,
        )
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_both_pages_have_print_class_sheet(self):
        summary = self._template("class_learning_objectives_summary.html")
        students = self._template("class_students.html")
        self.assertIn("Print class sheet", summary)
        self.assertIn("Print class sheet", students)
        self.assertIn("print student sheets", students)
        self.assertIn(
            "url_for('main.class_learning_objectives_summary', class_id=class_id, print=1)",
            students,
        )

    def test_summary_auto_print_is_only_for_print_param(self):
        src = self._template("class_learning_objectives_summary.html")
        self.assertIn('params.get("print") !== "1"', src)
        gate = src.find('params.get("print") !== "1"')
        load = src.find('addEventListener("load"', gate)
        timer = src.find("setTimeout", load)
        printed = src.find("window.print()", timer)
        delay = src.find("}, 250);", timer)
        replaced = src.find("history.replaceState", printed)
        self.assertGreater(load, gate)
        self.assertGreater(timer, load)
        self.assertGreater(printed, timer)
        self.assertGreater(delay, printed)
        self.assertGreater(replaced, printed)
        self.assertIn("lo-print-title", src)
        self.assertIn("lo-screen-header", src)
        self.assertNotIn("print:block", src)
        self.assertNotIn("print:hidden", src)
        title = src[src.find('class="lo-print-title"'):src.find("lo-screen-header")]
        self.assertIn("class_display_title(class_name, class_days, class_section)", title)
        self.assertNotIn("class_semester", title)
        self.assertNotIn("{{ class_name }}", title)
        self.assertNotIn("{{ class_days }}", title)

    def test_summary_print_rules_are_landscape_with_repeating_header(self):
        src = self._template("class_learning_objectives_summary.html")
        self.assertIn("@page { size: landscape;", src)
        self.assertIn("@page { size: landscape; margin: 0; }", src)
        self.assertIn("table-header-group", src)
        self.assertIn("table-row-group", src)
        self.assertNotIn("table-footer-group", src)
        self.assertIn("font-size: inherit !important", src)
        self.assertIn("td.text-green-700", src)
        self.assertIn("td.lo-progress", src)
        self.assertIn("bg-blue-600", src)
        self.assertIn("lo-print-page", src)
        self.assertIn("break-after: page", src)
        self.assertIn(".lo-print-pages { display: none; }", src)
        self.assertIn(".lo-print-pages { display: block !important; }", src)
        print_src = src.split('class="lo-print-pages"', 1)[1].split("lo-screen-header", 1)[0]
        self.assertEqual(print_src.count(">Passed<"), 1)
        self.assertEqual(print_src.count("{{ lo_passed_row() }}"), 1)

    def _summary_grid(self, count):
        los = [{
            "vendor_code": "T0",
            "name": "LO 0",
            "passed_count": 0,
        }]
        rows = []
        for index in range(count):
            rows.append({
                "name": "Student %s" % index,
                "cells": [{
                    "text": "0/2",
                    "status": "behind",
                    "display": "0/2",
                }],
                "progress_text": "0/1",
            })
        return los, rows

    def _render_summary(self, count):
        from flask import render_template, session
        los, rows = self._summary_grid(count)
        app = create_app()
        with app.test_request_context("/class/c1/learning-objectives"):
            session["role"] = "instructor"
            return render_template(
                "class_learning_objectives_summary.html",
                class_id="c1",
                class_name="Test Upload",
                class_days="MW",
                class_section="F26",
                class_semester="Fall 2026",
                printed_on="2026-10-20",
                learning_objectives=los,
                total_students=count,
                grid_rows=rows,
            )

    def test_forty_three_students_print_on_one_page(self):
        html = self._render_summary(43)
        self.assertEqual(html.count('class="lo-print-page"'), 1)

    def test_seventy_students_print_on_two_pages(self):
        html = self._render_summary(70)
        parts = html.split('class="lo-print-page"')
        self.assertEqual(len(parts) - 1, 2)
        self.assertNotIn(">Passed<", parts[1])
        self.assertIn(">Passed<", parts[2])

    def test_students_link_renders_print_query(self):
        from flask import render_template, session
        app = create_app()
        student = {
            "id": "s1",
            "name": "Lee, Ana",
            "email": "",
            "learning_objectives": [],
            "objective_total": 0,
        }
        with app.test_request_context("/class/c1/students"):
            session["role"] = "instructor"
            html = render_template(
                "class_students.html",
                class_id="c1",
                class_name="MW",
                students=[student],
            )
        self.assertIn("/class/c1/learning-objectives?print=1", html)
        self.assertIn("print student sheets", html)


class TestRevisionReminders(unittest.TestCase):
    def _client(self):
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst-1"
            sess["role"] = "instructor"
            sess["full_name"] = "Lovelace, Ada"
            sess["csrf_token"] = "test-csrf"
        return client

    def _partition(self):
        return {
            "due": "2026-10-24",
            "recipients": [{
                "student_id": "stu-1",
                "name": "Lee, Ana",
                "email": "ana@example.edu",
                "assignments": [{
                    "assignment_id": "a1",
                    "assignment_name": "Quiz",
                    "objectives": [{
                        "lo_code": "E2",
                        "lo_name": "Limits",
                        "show_name": True,
                        "top_score": "R",
                        "learning_objective_id": "lo-1",
                    }, {
                        "lo_code": "E3",
                        "lo_name": "Derivatives",
                        "show_name": True,
                        "top_score": "RQ",
                        "learning_objective_id": "lo-2",
                    }],
                }],
            }, {
                "student_id": "stu-2",
                "name": "Pat Kim",
                "email": "pat@example.edu",
                "assignments": [{
                    "assignment_id": "a1",
                    "assignment_name": "Quiz",
                    "objectives": [{
                        "lo_code": "E2",
                        "lo_name": "Limits",
                        "show_name": True,
                        "top_score": "RQ",
                        "learning_objective_id": "lo-1",
                    }],
                }],
            }],
            "no_email": [{"student_id": "stu-3", "name": "Cho, Cam"}],
            "already_reminded": [{"student_id": "stu-4", "name": "Diaz, Dia"}],
        }

    def _rows(self):
        return [
            _revision_row(
                "stu-1", "Lee, Ana", "2026-10-24", "Quiz", "E2", "Limits",
                assignment_id="a1", learning_objective_id="lo-1",
            ),
            _revision_row(
                "stu-1", "Lee, Ana", "2026-10-24", "Quiz", "E3", "Derivatives",
                assignment_id="a1", top_score="RQ", learning_objective_id="lo-2",
            ),
            _revision_row(
                "stu-1", "Lee, Ana", "2026-10-25", "Later", "E9", "Later objective",
                assignment_id="a9", learning_objective_id="lo-9",
            ),
            _revision_row(
                "stu-3", "Cho, Cam", "2026-10-24", "Quiz", "E2", "Limits",
                assignment_id="a1", learning_objective_id="lo-1",
            ),
            _revision_row(
                "stu-4", "Diaz, Dia", "2026-10-24", "Quiz", "E2", "Limits",
                assignment_id="a1", learning_objective_id="lo-1",
            ),
        ]

    def _students(self):
        return [
            {"id": "stu-1", "full_name": "Lee, Ana", "email": "ana@example.edu"},
            {"id": "stu-3", "full_name": "Cho, Cam", "email": "not-an-email"},
            {"id": "stu-4", "full_name": "Diaz, Dia", "email": "dia@example.edu"},
        ]

    def test_partition_keeps_only_unsent_items_due_in_three_days(self):
        from datetime import date
        from app import routes as r
        sent = {("stu-1", "a1", "lo-1"), ("stu-4", "a1", "lo-1")}
        with unittest.mock.patch.object(r, "_load_sent_reminder_keys", return_value=sent):
            part = r.revision_reminder_partition(
                "c1",
                date(2026, 10, 21),
                named_rows=self._rows(),
                students=self._students(),
            )
        self.assertEqual([row["student_id"] for row in part["recipients"]], ["stu-1"])
        objectives = part["recipients"][0]["assignments"][0]["objectives"]
        self.assertEqual([obj["learning_objective_id"] for obj in objectives], ["lo-2"])
        body = r._revision_message(
            part["recipients"][0], part["due"], "Algebra", "Professor Lovelace"
        )
        self.assertIn("Assignment: Quiz", body)
        self.assertIn("- E3 Derivatives: Revise with Question (RQ)", body)
        self.assertIn("E3 Derivatives: Revise with Question (RQ)", body)
        self.assertNotIn("E2", body)
        self.assertNotIn("Later", body)
        self.assertEqual([row["student_id"] for row in part["already_reminded"]], ["stu-4"])
        self.assertEqual([row["student_id"] for row in part["no_email"]], ["stu-3"])
        self.assertEqual(part["due"], "2026-10-24")

    def test_signoff_greeting_and_from_header(self):
        from app import routes as r
        self.assertEqual(r._revision_signoff("Lovelace, Ada"), "Ada Lovelace")
        self.assertEqual(r._revision_signoff("Ada Lovelace"), "Ada Lovelace")
        self.assertEqual(r.instructor_title("Lovelace, Ada"), "Professor Lovelace")
        self.assertEqual(r.instructor_title("Ada Lovelace"), "Professor Lovelace")
        self.assertEqual(r.instructor_title("brody estes"), "Professor Estes")
        self.assertEqual(r.instructor_title("Estes"), "Professor Estes")
        self.assertEqual(r.instructor_title(""), "Your instructor")
        self.assertEqual(
            r._reports_from_header("Ada Lovelace", "reports@claritygrader.net"),
            '"Ada Lovelace via Clarity Grader" <reports@claritygrader.net>',
        )
        quiz = {
            "assignment_name": "Quiz",
            "objectives": [{
                "lo_code": "E2",
                "lo_name": "Limits",
                "show_name": True,
                "top_score": "R",
            }],
        }
        homework = {
            "assignment_name": "Homework",
            "objectives": [{
                "lo_code": "E4",
                "lo_name": "Integrals",
                "show_name": True,
                "top_score": "R",
            }],
        }
        body = r._revision_message(
            {"name": "Pat Kim", "assignments": [quiz, homework]},
            "2026-10-24",
            "Algebra | MWF | 1",
            "Professor Lovelace",
        )
        self.assertIn("Hello Pat Kim,", body)
        self.assertIn("You have a revision due in 3 days, on Saturday, Oct 24.", body)
        self.assertIn("Class: Algebra | MWF | 1", body)
        self.assertIn("Assignment: Quiz", body)
        self.assertIn("- E2 Limits: Revise for Mastery", body)
        self.assertIn("Thank you,", body)
        self.assertIn("Professor Lovelace", body)
        self.assertIn(
            "Assignment: Quiz\n- E2 Limits: Revise for Mastery\n\nAssignment: Homework",
            body,
        )
        self.assertIn(
            "Replies to this email are not monitored. Contact your instructor directly.",
            body,
        )

    def test_preview_returns_one_sample_and_escapes_it(self):
        from app import routes as r
        partition = self._partition()
        partition["recipients"][0]["name"] = "Lee, <b>x</b>"
        client = self._client()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "revision_reminder_partition", return_value=partition), \
                unittest.mock.patch.object(r, "_revision_class_title", return_value="Algebra"), \
                unittest.mock.patch.object(r, "_send_via_resend") as sender:
            rv = client.post(
                "/api/class/c1/revision-reminders/preview",
                json={},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 200)
        payload = rv.get_json()
        self.assertNotIn("body", payload["recipients"][0])
        self.assertEqual(payload["recipients"][0]["email"], "ana@example.edu")
        self.assertEqual([row["student_id"] for row in payload["no_email"]], ["stu-3"])
        self.assertIn("Class: Algebra", payload["sample_body"])
        self.assertIn("Hello <b>x</b>,", payload["sample_body"])
        self.assertIn("Revise for Mastery", payload["sample_body"])
        self.assertIn("Revise with Question (RQ)", payload["sample_body"])
        self.assertIn("Professor Lovelace", payload["sample_body"])
        self.assertIn("Assignment: Quiz", payload["sample_body"])
        self.assertIn("- E2 Limits: Revise for Mastery", payload["sample_body"])
        self.assertIn("Thank you,", payload["sample_body"])
        self.assertIn("E2", payload["sample_body"])
        self.assertIn("E3", payload["sample_body"])
        html = r._report_body_html(payload["sample_body"], payload["subject"])
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)
        self.assertNotIn("<b>x</b>", html)
        sender.assert_not_called()

    def test_send_ignores_a_body_list_and_pauses_between_sends(self):
        from app import routes as r
        inserted = []
        client = self._client()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "revision_reminder_partition", return_value=self._partition()), \
                unittest.mock.patch.object(r, "_revision_class_title", return_value="Algebra"), \
                unittest.mock.patch.object(r, "_insert_reminder_rows", side_effect=lambda *args: inserted.append(args[1]["student_id"])), \
                unittest.mock.patch.object(r.time, "sleep") as sleeper, \
                unittest.mock.patch.object(r, "_send_via_resend", return_value=(True, "")) as sender, \
                unittest.mock.patch.dict(os.environ, {"RESEND_API_KEY": "k", "REPORTS_FROM_EMAIL": "a@b.c"}):
            rv = client.post(
                "/api/class/c1/revision-reminders/send",
                json={"recipients": [{"email": "intruder@example.edu"}]},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(sender.call_count, 2)
        self.assertEqual(inserted, ["stu-1", "stu-2"])
        self.assertNotIn("intruder@example.edu", str(sender.call_args_list))
        self.assertEqual(sleeper.call_args_list[0].args[0], 0.5)
        self.assertEqual(rv.get_json()["counts"]["skipped"], 1)
        self.assertFalse(sender.call_args.kwargs["log_provider_body"])
        self.assertEqual(sender.call_args.kwargs["sender_name"], "Ada Lovelace")

    def test_second_send_skips_students_already_reminded(self):
        from app import routes as r
        partition = self._partition()
        partition["already_reminded"] = partition["already_reminded"] + [
            {"student_id": "stu-1", "name": "Lee, Ana"},
            {"student_id": "stu-2", "name": "Pat Kim"},
        ]
        partition["recipients"] = []
        client = self._client()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "revision_reminder_partition", return_value=partition), \
                unittest.mock.patch.object(r, "_revision_class_title", return_value="Algebra"), \
                unittest.mock.patch.object(r, "_send_via_resend") as sender, \
                unittest.mock.patch.dict(os.environ, {"RESEND_API_KEY": "k", "REPORTS_FROM_EMAIL": "a@b.c"}):
            rv = client.post(
                "/api/class/c1/revision-reminders/send",
                json={},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 200)
        sender.assert_not_called()
        self.assertEqual(rv.get_json()["counts"]["skipped"], 3)

    def test_one_failure_does_not_stop_the_rest_or_record_the_failure(self):
        from app import routes as r
        inserted = []

        def send(to_email, subject, body_text, *, sender_name, log_provider_body=True):
            return (to_email != "ana@example.edu", "")

        client = self._client()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "revision_reminder_partition", return_value=self._partition()), \
                unittest.mock.patch.object(r, "_revision_class_title", return_value="Algebra"), \
                unittest.mock.patch.object(r, "_insert_reminder_rows", side_effect=lambda *args: inserted.append(args[1]["student_id"])), \
                unittest.mock.patch.object(r.time, "sleep"), \
                unittest.mock.patch.object(r, "_send_via_resend", side_effect=send), \
                unittest.mock.patch.dict(os.environ, {"RESEND_API_KEY": "k", "REPORTS_FROM_EMAIL": "a@b.c"}):
            rv = client.post(
                "/api/class/c1/revision-reminders/send",
                json={},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        payload = rv.get_json()
        self.assertEqual(payload["counts"]["failed"], 1)
        self.assertEqual(payload["counts"]["sent"], 1)
        self.assertEqual(inserted, ["stu-2"])

    def test_budget_marks_the_rest_not_attempted(self):
        from app import routes as r
        clock = {"now": 0.0}

        def monotonic():
            return clock["now"]

        def sleep(seconds):
            clock["now"] += seconds

        def send(*args, **kwargs):
            clock["now"] += 40
            return True, ""

        partition = self._partition()
        third = dict(partition["recipients"][1])
        third["student_id"] = "stu-9"
        third["email"] = "nine@example.edu"
        third["assignments"] = partition["recipients"][1]["assignments"]
        partition["recipients"] = partition["recipients"] + [third]
        client = self._client()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "revision_reminder_partition", return_value=partition), \
                unittest.mock.patch.object(r, "_revision_class_title", return_value="Algebra"), \
                unittest.mock.patch.object(r, "_insert_reminder_rows"), \
                unittest.mock.patch.object(r.time, "monotonic", side_effect=monotonic), \
                unittest.mock.patch.object(r.time, "sleep", side_effect=sleep), \
                unittest.mock.patch.object(r, "_send_via_resend", side_effect=send), \
                unittest.mock.patch.dict(os.environ, {"RESEND_API_KEY": "k", "REPORTS_FROM_EMAIL": "a@b.c"}):
            rv = client.post(
                "/api/class/c1/revision-reminders/send",
                json={},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        payload = rv.get_json()
        self.assertEqual(r._REVISION_EMAIL_BUDGET_SECONDS, 60)
        self.assertEqual(payload["counts"]["sent"], 2)
        self.assertEqual(payload["counts"]["not_attempted"], 1)
        self.assertEqual(
            payload["message"],
            "Close this window and choose Email students again to send the rest.",
        )

    def test_insert_failure_is_sent_not_recorded(self):
        from app import routes as r
        client = self._client()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "revision_reminder_partition", return_value=self._partition()), \
                unittest.mock.patch.object(r, "_revision_class_title", return_value="Algebra"), \
                unittest.mock.patch.object(r, "_insert_reminder_rows", side_effect=RuntimeError("db")), \
                unittest.mock.patch.object(r.time, "sleep"), \
                unittest.mock.patch.object(r, "_send_via_resend", return_value=(True, "")), \
                unittest.mock.patch.dict(os.environ, {"RESEND_API_KEY": "k", "REPORTS_FROM_EMAIL": "a@b.c"}):
            with self.assertLogs("app.routes", level="ERROR") as logs:
                rv = client.post(
                    "/api/class/c1/revision-reminders/send",
                    json={},
                    headers={"X-CSRF-Token": "test-csrf"},
                )
        payload = rv.get_json()
        self.assertEqual(payload["counts"]["sent_not_recorded"], 2)
        self.assertIn("may email that student twice", payload["warning"])
        self.assertNotIn("@", "\n".join(logs.output))

    def test_upsert_ignores_duplicate_reminder_rows(self):
        from app import routes as r
        query = MagicMock()
        admin = MagicMock()
        admin.table.return_value = query
        query.upsert.return_value = query
        student = {
            "student_id": "stu-1",
            "assignments": [{
                "assignment_id": "a1",
                "objectives": [{"learning_objective_id": "lo-1"}],
            }],
        }
        with unittest.mock.patch.object(r, "supabase_admin", admin):
            r._insert_reminder_rows("c1", student, "2026-10-24", "inst-1")
        args, kwargs = query.upsert.call_args
        self.assertEqual(args[0][0]["assignment_id"], "a1")
        self.assertEqual(args[0][0]["learning_objective_id"], "lo-1")
        self.assertTrue(kwargs["ignore_duplicates"])
        self.assertEqual(
            kwargs["on_conflict"],
            "student_id,assignment_id,learning_objective_id,revision_due",
        )
        query.execute.assert_called_once()
        broken = {
            "student_id": "stu-1",
            "assignments": [{"assignment_id": "a1", "objectives": [{}]}],
        }
        with self.assertRaises(KeyError):
            r._insert_reminder_rows("c1", broken, "2026-10-24", "inst-1")

    def test_provider_429_is_failed_and_logs_no_address(self):
        from app import routes as r
        resp = MagicMock()
        resp.status_code = 429
        resp.json.return_value = {"message": "slow down ana@example.edu"}
        resp.text = "slow down ana@example.edu"
        with unittest.mock.patch.dict(os.environ, {"RESEND_API_KEY": "k", "REPORTS_FROM_EMAIL": "a@b.c"}), \
                unittest.mock.patch.object(r.requests, "post", return_value=resp) as posted, \
                self.assertLogs("app.routes", level="ERROR") as logs:
            ok, err = r._send_via_resend(
                "ana@example.edu",
                "Subject",
                "Hello",
                sender_name="Ada Lovelace",
                log_provider_body=False,
            )
        self.assertFalse(ok)
        self.assertEqual(err, "Email provider rejected request.")
        self.assertNotIn("ana@example.edu", "\n".join(logs.output))
        self.assertNotIn("reply_to", posted.call_args.kwargs["json"])

    def test_non_owner_and_missing_csrf_are_forbidden(self):
        from app import routes as r
        client = self._client()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=False):
            owned = client.post(
                "/api/class/c1/revision-reminders/preview",
                json={},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        missing = client.post("/api/class/c1/revision-reminders/send", json={})
        self.assertEqual(owned.status_code, 403)
        self.assertEqual(missing.status_code, 403)

    def test_missing_email_config_sends_nothing(self):
        from app import routes as r
        client = self._client()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_send_via_resend") as sender, \
                unittest.mock.patch.dict(os.environ, {"RESEND_API_KEY": "", "REPORTS_FROM_EMAIL": ""}, clear=False):
            rv = client.post(
                "/api/class/c1/revision-reminders/send",
                json={},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 503)
        sender.assert_not_called()

    def _page(self, rows, students, reminder_rows=None, reminder_error=False):
        from app import routes as r
        from datetime import date
        calls = []

        class Query:
            def __init__(self, name):
                self.name = name

            def select(self, *args, **kwargs):
                return self

            def eq(self, *args, **kwargs):
                return self

            def execute(self):
                calls.append(self.name)
                if self.name == "revision_reminders_sent" and reminder_error:
                    raise RuntimeError("reminders down")
                result = MagicMock()
                result.data = reminder_rows or []
                return result

        class_data = {
            "id": "c1",
            "name": "Algebra",
            "days": "MWF",
            "section_number": "1",
            "enrollments": [],
            "learning_objectives": [],
        }
        ready = []
        for student in students:
            item = dict(student)
            item.setdefault("name", item.get("full_name") or "")
            item.setdefault("objective_total", 0)
            item.setdefault("learning_objectives", [])
            ready.append(item)
        client = self._client()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r.Course, "get_full_class_data", return_value=class_data), \
                unittest.mock.patch.object(r.Course, "get_learning_objectives", return_value=[]), \
                unittest.mock.patch.object(r, "_attached_objective_ids", return_value=set()), \
                unittest.mock.patch.object(r, "load_assignments_for_class", return_value=[]), \
                unittest.mock.patch.object(r, "_process_enrollments", return_value=(ready, ready, {})), \
                unittest.mock.patch.object(r.Grade, "get_revision_candidates", return_value=rows), \
                unittest.mock.patch.object(r, "local_today", return_value=date(2026, 10, 21)), \
                unittest.mock.patch.object(r, "supabase_admin") as admin:
            admin.table.side_effect = Query
            rv = client.get("/class/c1")
        return rv, calls

    def test_counter_matches_preview_recipients(self):
        from app import routes as r
        from datetime import date
        rows = [
            _revision_row(
                "stu-1", "Lee, Ana", "2026-10-24", "Quiz", "E2", "Limits",
                assignment_id="a1", learning_objective_id="lo-1",
            ),
            _revision_row(
                "stu-3", "Cho, Cam", "2026-10-24", "Quiz", "E2", "Limits",
                assignment_id="a1", learning_objective_id="lo-1",
            ),
        ]
        students = [
            {"id": "stu-1", "full_name": "Lee, Ana", "email": "ana@example.edu"},
            {"id": "stu-3", "full_name": "Cho, Cam", "email": ""},
        ]
        page, calls = self._page(rows, students, reminder_rows=[])
        self.assertEqual(page.status_code, 200)
        text = page.get_data(as_text=True)
        self.assertIn("Due in 3 days ★ (1 not emailed)", text)
        self.assertIn("Email students (1)", text)
        self.assertEqual(calls.count("revision_reminders_sent"), 1)
        client = self._client()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r.Course, "get_full_class_data", return_value={"id": "c1", "name": "Algebra", "enrollments": [], "learning_objectives": []}), \
                unittest.mock.patch.object(r, "_attached_objective_ids", return_value=set()), \
                unittest.mock.patch.object(r, "_process_enrollments", return_value=(students, students, {})), \
                unittest.mock.patch.object(r.Grade, "get_revision_candidates", return_value=rows), \
                unittest.mock.patch.object(r, "local_today", return_value=date(2026, 10, 21)), \
                unittest.mock.patch.object(r, "_load_sent_reminder_keys", return_value=set()), \
                unittest.mock.patch.object(r, "_revision_class_title", return_value="Algebra"):
            preview = client.post(
                "/api/class/c1/revision-reminders/preview",
                json={},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(len(preview.get_json()["recipients"]), 1)
        self.assertNotIn("body", preview.get_json()["recipients"][0])

    def test_counter_drops_after_a_recorded_send(self):
        rows = [
            _revision_row(
                "stu-1", "Lee, Ana", "2026-10-24", "Quiz", "E2", "Limits",
                assignment_id="a1", learning_objective_id="lo-1",
            ),
        ]
        students = [{"id": "stu-1", "full_name": "Lee, Ana", "email": "ana@example.edu"}]
        sent = [{
            "student_id": "stu-1",
            "assignment_id": "a1",
            "learning_objective_id": "lo-1",
        }]
        page, _calls = self._page(rows, students, reminder_rows=sent)
        text = page.get_data(as_text=True)
        self.assertIn("All students have been emailed.", text)
        self.assertNotIn("not emailed", text)
        self.assertNotIn("Email students (", text)

    def test_zero_count_has_no_counter_text_for_no_email(self):
        rows = [
            _revision_row(
                "stu-3", "Cho, Cam", "2026-10-24", "Quiz", "E2", "Limits",
                assignment_id="a1", learning_objective_id="lo-1",
            ),
        ]
        students = [{"id": "stu-3", "full_name": "Cho, Cam", "email": "not-an-email"}]
        page, _calls = self._page(rows, students, reminder_rows=[])
        text = page.get_data(as_text=True)
        self.assertIn("No students have a usable email address.", text)
        self.assertNotIn("not emailed", text)

    def test_page_renders_when_the_reminders_query_raises(self):
        rows = [
            _revision_row(
                "stu-1", "Lee, Ana", "2026-10-24", "Quiz", "E2", "Limits",
                assignment_id="a1", learning_objective_id="lo-1",
            ),
        ]
        students = [{"id": "stu-1", "full_name": "Lee, Ana", "email": "ana@example.edu"}]
        page, calls = self._page(rows, students, reminder_error=True)
        text = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("not emailed", text)
        self.assertIn("Email students", text)
        self.assertEqual(calls.count("revision_reminders_sent"), 1)

    def test_template_send_stays_disabled_and_close_reloads(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "templates", "class_detail.html",
        )
        with open(path, encoding="utf-8") as handle:
            src = handle.read()
        button_at = src.find('id="revisionEmailBtn"')
        self.assertIn("view.key == '3'", src[button_at - 400:button_at])
        send_fn = src.split("function sendRevisionEmails()", 1)[1]
        self.assertIn("emailSendBtn.disabled = true", send_fn)
        self.assertIn("1 student will be emailed.", src)
        self.assertIn("Sample message (first student)", src)
        self.assertIn("if (missingEmail.length)", src)
        self.assertIn("if (reminded.length)", src)
        self.assertIn("people.length === 0", src)
        self.assertIn("location.reload()", src)
        self.assertIn(
            "Close this window and choose Email students again to send the rest.",
            open(os.path.join(os.path.dirname(__file__), "..", "app", "routes.py"), encoding="utf-8").read(),
        )
        self.assertNotIn("_REVISION_EMAIL_CAP", open(
            os.path.join(os.path.dirname(__file__), "..", "app", "routes.py"),
            encoding="utf-8",
        ).read())
        self.assertNotIn("over_limit", src)


if __name__ == '__main__':
    unittest.main()
