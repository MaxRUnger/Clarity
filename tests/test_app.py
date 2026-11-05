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

sys.modules.setdefault('app.helpers.authentication', MagicMock(
    supabase=_mock_supabase,
    supabase_admin=_mock_supabase_admin,
))

from app.models import (
    Course,
    Grade,
    Homework,
    MASTERY_GRADES,
    DEFAULT_REQUIRED_MS,
)
from app.helpers.csv_text import (
    _GRADESHEET_HEADER_ERROR,
    parse_blank_gradesheet_csv_text,
    parse_students_csv_text,
)
from app.helpers.gradesheet_csv import (
    _BLANK_GRADESHEET_NOTE,
    _build_gradesheet_csv_text,
    _csv_format_hw_score,
    _gradesheet_csv_data_rows,
    _gradesheet_letter_map_from_rows,
)
from app.helpers.class_access import (
    _assignment_belongs_to_class,
    _instructor_owns_class,
)
from app.helpers.class_data import (
    _gradesheet_export_bundle,
    organize_by_learning_objectives,
)
from app.helpers.class_progress import (
    _aggregate_lo_grades,
    _process_enrollments,
    normalize_profile,
    objectives_attached_to_assignments,
)
from app.helpers.lo_import import _insert_learning_objectives
from app.helpers.report_email import (
    _send_via_resend,
    instructor_title,
)
from app import create_app
from app.helpers.student_names import display_name, row_sort_key
from tests.routes.fakes import _ArchiveBucket, _RemovalQuery, _revision_row
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
        from app.helpers.revision_report import _sort_overdue_revisions
        rows = [
            {"student_name": "Zenith, Bob"},
            {"student_name": "Adams, Zoe"},
        ]
        ordered = _sort_overdue_revisions(rows)
        self.assertEqual(
            [row["student_name"] for row in ordered],
            ["Adams, Zoe", "Zenith, Bob"],
        )


class TestRevisionReport(unittest.TestCase):
    def test_local_today_at_02_utc_is_the_previous_pacific_date(self):
        from datetime import datetime, timezone, date
        from app.helpers.local_time import local_today
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
        from app.helpers.revision_report import build_revision_report
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
        from app.helpers.local_time import local_today
        from app.helpers.revision_report import build_revision_report
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
        from app.helpers.revision_report import build_revision_report
        report = build_revision_report([], date(2026, 10, 20))
        views = {view["key"]: view for view in report["views"]}
        self.assertEqual(views["4"]["empty_message"], "No revisions due in 4 days")
        self.assertEqual(views["4"]["label"], "Due in 4 days")
        self.assertEqual(views["overdue"]["revision_count"], 0)

    def test_assignments_group_by_id_and_objectives_sort_by_code(self):
        from datetime import date
        from app.helpers.revision_report import build_revision_report
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
                "classes/class_detail.html",
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
        from app.helpers.revision_report import build_revision_report
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
            os.path.dirname(__file__), "..", "app", "templates", "classes", "class_detail.html",
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
        sa, calls = self._patch_supabase_admin({
            "classes": [{"instructor_id": "u1"}],
        })
        from app.helpers import class_access
        with self.app.test_request_context("/"):
            with unittest.mock.patch("app.helpers.class_access.supabase_admin", sa):
                a = class_access._class_instructor_id("c1")
                b = class_access._class_instructor_id("c1")
                self.assertEqual(a, "u1")
                self.assertEqual(b, "u1")
                self.assertEqual(calls["total"], 1)

    def test_class_instructor_id_non_string_is_not_owner(self):
        from flask import session

        for raw in (7, None):
            sa, _calls = self._patch_supabase_admin({
                "classes": [{"instructor_id": raw}],
            })
            from app.helpers import class_access
            with self.app.test_request_context("/"):
                with unittest.mock.patch("app.helpers.class_access.supabase_admin", sa):
                    session["user_id"] = "u1"
                    self.assertIsNone(class_access._class_instructor_id("c1"))
                    self.assertFalse(_instructor_owns_class("c1"))

    def test_assignment_belongs_to_class_memoized(self):
        sa, calls = self._patch_supabase_admin({
            "assignments": [{"id": "a1"}],
        })
        with self.app.test_request_context("/"):
            with unittest.mock.patch("app.helpers.class_access.supabase_admin", sa):
                self.assertTrue(_assignment_belongs_to_class("c1", "a1"))
                self.assertTrue(_assignment_belongs_to_class("c1", "a1"))
                self.assertEqual(calls["total"], 1)


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
        from app.helpers.api_input import _require_json_object

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
        from app.helpers.api_input import _require_json_object

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
        from app.helpers.api_input import _require_json_object

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
        from app.helpers.api_input import _require_json_object

        with self.app.test_request_context(
            "/dummy",
            method="POST",
            data='{"rows": []}',
            content_type="application/json",
        ):
            data, err = _require_json_object()
            self.assertIsNone(err)
            self.assertEqual(data, {"rows": []})


class TestInstructorTitleAndReportEmail(unittest.TestCase):
    FROM_EMAIL = "reports@claritygrader.net"

    def test_instructor_title(self):
        self.assertEqual(instructor_title("Estes, Brody"), "Professor Estes")
        self.assertEqual(instructor_title("Brody Estes"), "Professor Estes")
        self.assertEqual(instructor_title(""), "Your instructor")
        self.assertEqual(instructor_title(None), "Your instructor")
        self.assertEqual(instructor_title("  Estes ,   Brody  "), "Professor Estes")
        self.assertEqual(instructor_title("  Brody    Estes  "), "Professor Estes")
        self.assertEqual(instructor_title("brody estes"), "Professor Estes")
        self.assertEqual(instructor_title("BRODY ESTES"), "Professor ESTES")

    def test_from_header_is_always_quoted(self):
        from app.helpers.report_email import _reports_from_header
        self.assertEqual(
            _reports_from_header("Professor Estes", self.FROM_EMAIL),
            '"Professor Estes via Clarity Grader" <reports@claritygrader.net>',
        )
        self.assertEqual(
            _reports_from_header("Your instructor", self.FROM_EMAIL),
            '"Your instructor via Clarity Grader" <reports@claritygrader.net>',
        )
        self.assertEqual(
            _reports_from_header("", self.FROM_EMAIL),
            '"Your instructor via Clarity Grader" <reports@claritygrader.net>',
        )
        self.assertEqual(
            _reports_from_header("Professor St. Estes", self.FROM_EMAIL),
            '"Professor St. Estes via Clarity Grader" <reports@claritygrader.net>',
        )
        self.assertEqual(
            _reports_from_header("Professor Estes, Jr.", self.FROM_EMAIL),
            '"Professor Estes, Jr. via Clarity Grader" <reports@claritygrader.net>',
        )

    def _send(self, post_mock, *, sender_name="Professor Estes", body="Hello Jane,"):
        with unittest.mock.patch.dict(
            os.environ,
            {
                "RESEND_API_KEY": "test-key",
                "REPORTS_FROM_EMAIL": self.FROM_EMAIL,
            },
        ), unittest.mock.patch("app.helpers.report_email.requests.post", post_mock):
            return _send_via_resend(
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
        with self.assertLogs("app.helpers.report_email", level="INFO") as logs:
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
        import requests
        post = MagicMock(side_effect=requests.Timeout())
        ok, err = self._send(post)
        self.assertFalse(ok)
        self.assertEqual(err, "Email send failed.")

    def test_send_missing_key_does_not_post(self):
        post = MagicMock()
        with unittest.mock.patch.dict(os.environ, {"REPORTS_FROM_EMAIL": self.FROM_EMAIL}):
            os.environ.pop("RESEND_API_KEY", None)
            with unittest.mock.patch("app.helpers.report_email.requests.post", post):
                ok, err = _send_via_resend(
                    "student@example.edu",
                    "Subject",
                    "Hello Jane,",
                    sender_name="Professor Estes",
                )
        self.assertFalse(ok)
        self.assertEqual(err, "Email is not configured.")
        post.assert_not_called()

    def test_send_missing_from_address_does_not_post(self):
        post = MagicMock()
        with unittest.mock.patch.dict(os.environ, {"RESEND_API_KEY": "test-key"}):
            os.environ.pop("REPORTS_FROM_EMAIL", None)
            with unittest.mock.patch("app.helpers.report_email.requests.post", post):
                ok, err = _send_via_resend(
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
        with self.assertLogs("app.helpers.report_email", level="INFO"):
            ok, err = self._send(post, body=body)
        self.assertTrue(ok)
        self.assertEqual(err, "")
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["text"], body)
        self.assertIn("Hello &lt;Jane&gt; &amp; Co,", sent["html"])
        self.assertNotIn("<Jane>", sent["html"])

    def test_report_body_html_paragraphs_breaks_and_footer(self):
        from app.helpers.report_email import _report_body_html
        body = (
            "Hello <Jane> & Co,\n"
            "Class: Algebra\n"
            "\n"
            "D1  |  M\n"
            "D2  |  R\n"
            "\n"
            "Professor Estes\n"
            "Class: Algebra\n"
            "You are receiving this because you are enrolled in Algebra.\n"
            "Replies to this email are not monitored. "
            "Contact your instructor directly."
        )
        html = _report_body_html(body, "Progress <report> & more")
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
        self.assertIn("Professor Estes<br>", paragraphs[2])
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
        from app.helpers.gradesheet_lookup import _gradesheet_letter_map_for_assignment

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

        with unittest.mock.patch("app.helpers.gradesheet_lookup.supabase_admin", _Admin()):
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
        self.assertEqual(rows[1], ["Smith, Alex", "-1", "0", ""])
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
        with unittest.mock.patch("app.helpers.class_data.supabase_admin") as sa, \
                unittest.mock.patch.object(
                    Course, "get_full_class_data", return_value={"name": "C"}
                ), \
                unittest.mock.patch(
                    "app.helpers.class_data._process_enrollments",
                    return_value=([{"id": "stu-1", "full_name": "Doe, Jane"}], [], {}),
                ), \
                unittest.mock.patch.object(
                    Course, "get_learning_objectives", return_value=pool
                ), \
                unittest.mock.patch.object(
                    Course, "get_assignment_lo_ids", return_value=["lo-2", "lo-4"]
                ), \
                unittest.mock.patch.object(
                    Homework, "get_hw_scores_map_for_assignment", return_value={"stu-1": 80}
                ):
            sa.table.return_value = q
            bundle = _gradesheet_export_bundle("class-1", "asg-1")

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
        from app.helpers.class_progress import _process_enrollments
        client, students = self._client(True)
        with unittest.mock.patch("app.models.supabase_admin", client):
            class_data = Course.get_full_class_data("class-mw")
        with unittest.mock.patch(
            "app.helpers.class_progress.objectives_attached_to_assignments",
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

        with unittest.mock.patch(
            "app.helpers.lo_import.supabase_admin", _Client()
        ):
            result = _insert_learning_objectives(payload)
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

        with unittest.mock.patch(
            "app.helpers.lo_import.supabase_admin", _Client()
        ):
            with self.assertRaises(RuntimeError):
                _insert_learning_objectives({
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
        from app.helpers.class_progress import _process_enrollments
        active, _, _ = _process_enrollments(self._class_data(self._pool(4), []))
        student = active[0]
        self.assertEqual(student["objective_total"], 4)
        self.assertEqual(student["learning_objectives"], [])
        html = self._render_card(student)
        self.assertIn("0 / 4", html)
        self.assertEqual(html.count("lo-row"), 0)

    def test_partial_grades_use_class_total(self):
        from app.helpers.class_progress import _process_enrollments
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
        from app.helpers.class_progress import _process_enrollments
        active, _, _ = _process_enrollments(self._class_data([], []))
        student = active[0]
        self.assertEqual(student["objective_total"], 0)
        self.assertEqual(student["learning_objectives"], [])
        html = self._render_card(student)
        self.assertIn("0 / 0", html)

    def test_outside_pool_grade_does_not_raise_passed(self):
        from app.helpers.class_progress import class_lo_lookup, class_progress
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
                "classes/class_students.html",
                class_id="c1",
                class_name="MW",
                students=[student],
            )
        self.assertIn("0 / 4 objectives passed", html)

    def test_enrollment_and_detail_share_total(self):
        from app.helpers.class_progress import _process_enrollments, class_lo_lookup, class_progress
        pool = self._pool(2)
        grades = []
        active, _, _lookup = _process_enrollments(self._class_data(pool, grades))
        _rows, total = class_progress(grades, class_lo_lookup(pool))
        self.assertEqual(active[0]["objective_total"], total)
        self.assertEqual(total, 2)

    def test_lookup_skips_missing_id_and_empty_progress(self):
        from app.helpers.class_progress import class_lo_lookup, class_progress
        lookup = class_lo_lookup([
            {"id": "keep", "name": "Keep", "vendor_code": "K", "required_ms": 2},
            {"name": "No id"},
        ])
        self.assertEqual(set(lookup), {"keep"})
        grades = self._passing("keep")
        self.assertEqual(class_progress(grades, {}), ([], 0))


class TestLoSortKey(unittest.TestCase):
    def _codes(self, rows):
        from app.helpers.lo_order import lo_sort_key
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
        from app.helpers.lo_order import lo_sort_key
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
        from app.helpers.lo_order import lo_sort_key
        rows = [
            {"id": "b", "vendor_code": "AO1"},
            {"id": "a", "vendor_code": "AO1"},
        ]
        self.assertEqual(
            [row["id"] for row in sorted(rows, key=lo_sort_key)],
            ["a", "b"],
        )


class TestClassDisplayTitle(unittest.TestCase):
    def test_name_days_and_section(self):
        from flask import render_template, session
        from app.helpers.class_form import CLASS_DAYS, class_display_title
        title = class_display_title("Test Upload", "MW", "F26")
        self.assertEqual(title, "Test Upload | MW | F26")
        app = create_app()
        with app.test_request_context("/instructor/dashboard"):
            session["role"] = "instructor"
            html = render_template(
                "classes/instructor_select_class.html",
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
        from app.helpers.class_form import class_display_title
        self.assertEqual(class_display_title("Test Upload", "MW", "  "), "Test Upload | MW")
        self.assertEqual(class_display_title("Test Upload", "MW", None), "Test Upload | MW")

    def test_blank_days_and_section_is_name_only(self):
        from app.helpers.class_form import class_display_title
        self.assertEqual(class_display_title("  Test Upload  ", "", None), "Test Upload")

    def test_archive_button_carries_the_escaped_raw_name(self):
        from flask import render_template, session
        from app.helpers.class_form import CLASS_DAYS
        app = create_app()
        with app.test_request_context("/instructor/dashboard"):
            session["role"] = "instructor"
            html = render_template(
                "classes/instructor_select_class.html",
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
        from app.helpers.class_form import CLASS_DAYS
        return self._render(
            "classes/instructor_select_class.html",
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
        from app.helpers.revision_report import build_revision_report
        return self._render(
            "classes/class_detail.html",
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
                "classes/class_students.html",
                {"class_id": "c1", "students": [student]},
            ),
            (
                "classes/class_learning_objectives_summary.html",
                {
                    "class_id": "c1",
                    "learning_objectives": [],
                    "grid_rows": [],
                    "total_students": 0,
                },
            ),
            (
                "classes/class_student_detail.html",
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
            "classes/class_student_detail.html",
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
            "classes/student_history.html",
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
            "classes/student_history.html",
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
            "classes/class_reports.html",
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


class TestAttachedObjectives(unittest.TestCase):
    def _clear_cache(self):
        from app.helpers.request_cache import _request_cache
        _request_cache().clear()

    def _passing(self, lo_id):
        return [
            {"learning_objective_id": lo_id, "top_score": "M", "counts_for_mastery": True},
            {"learning_objective_id": lo_id, "top_score": "M", "counts_for_mastery": True},
        ]

    def _run(self, class_id, pool, grades, link_rows, students=1):
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
            with unittest.mock.patch("app.helpers.class_progress.supabase_admin") as sa:
                sa.table.side_effect = lambda name: _Query(name)
                active, _, _ = _process_enrollments(class_data)
                attached = objectives_attached_to_assignments(class_id, pool)
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
            os.path.dirname(__file__), "..", "app", "templates", "classes", "class_assignments.html"
        )
        with open(path, encoding="utf-8") as handle:
            src = handle.read()
        message = src.index("not attached to an assignment yet")
        wrapper = src.index('id="assignmentsExistingLosListWrapper"')
        self.assertLess(message, wrapper)
        self.assertIn("{% if unattached_count %}", src)

    def test_learning_objectives_card_links_to_the_summary_page(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "templates", "classes", "class_detail.html"
        )
        with open(path, encoding="utf-8") as handle:
            src = handle.read()
        self.assertIn("total_los = learning_objectives|length", src)
        self.assertIn("main.class_learning_objectives_summary", src)

    def test_save_confirms_only_when_removed_objectives_have_grades(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "templates", "classes", "class_assignments.html"
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

    def _snapshot_gzip(self, students, assignments):
        import gzip
        import json
        document = {
            "format_version": 1,
            "archived_at": "2026-10-24T00:00:00+00:00",
            "classes": [{"id": "c1", "name": "Archive Class"}],
            "learning_objectives": [],
            "assignments": [{"id": "a%s" % index} for index in range(assignments)],
            "assignment_objectives": [],
            "enrollments": [
                {"id": "e%s" % index, "student_id": "s%s" % index}
                for index in range(students)
            ],
            "grades": [],
            "homework_scores": [],
            "free_passes": [],
            "students": [
                {"id": "s%s" % index, "full_name": "Student %s" % index}
                for index in range(students)
            ],
        }
        return gzip.compress(json.dumps(document, sort_keys=True).encode("utf-8"))

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
        snapshot_error=None,
        snapshot_override=None,
    ):
        from uuid import UUID
        from app.routes import archives as r
        from app.helpers.rate_limit import _rate_limit_events
        import app.helpers.archive_storage as storage

        _rate_limit_events.clear()
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

        def build_snapshot(_class_id, manifest):
            if snapshot_error is not None:
                raise snapshot_error
            if snapshot_override is not None:
                return snapshot_override
            return self._snapshot_gzip(
                manifest.get("source_students") or 0,
                manifest.get("source_assignments") or 0,
            )

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
            unittest.mock.patch("app.helpers.class_access.supabase_admin", admin),
            unittest.mock.patch.object(storage, "supabase_admin", admin),
            unittest.mock.patch("app.helpers.archive_export.build_class_archive_zip", side_effect=build_zip),
            unittest.mock.patch("app.helpers.archive_snapshot.build_class_snapshot", side_effect=build_snapshot),
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
        late, late_log, late_bucket, _deleted = self._post(monotonic=[0, 100, 201])
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
        self.assertEqual(
            [call[0] for call in bucket.calls],
            ["upload", "upload", "download", "download", "remove", "remove"],
        )
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
            ["upload", "upload", "download", "download", "insert", "delete_class_rows"],
        )
        self.assertEqual(
            [call[0] for call in bucket.calls],
            ["upload", "upload", "download", "download"],
        )
        path = f"inst1/{self.ARCHIVE_ID}.zip"
        snapshot_path = f"inst1/{self.ARCHIVE_ID}.snapshot.json.gz"
        self.assertEqual(bucket.calls[0][1], path)
        self.assertEqual(bucket.calls[0][2], zip_bytes)
        self.assertEqual(bucket.calls[0][3], {"content-type": "application/zip", "upsert": "false"})
        self.assertEqual(bucket.calls[1][1], snapshot_path)
        self.assertEqual(
            bucket.calls[1][3],
            {"content-type": "application/gzip", "upsert": "false"},
        )
        self.assertIsNot(bucket.calls[0][3], bucket.calls[1][3])
        import gzip
        import json
        import zipfile
        with zipfile.ZipFile(io.BytesIO(bucket.calls[0][2])) as archive:
            names = archive.namelist()
        self.assertFalse(any(name.endswith(".json") or name.endswith(".gz") for name in names))
        snapshot_text = gzip.decompress(bucket.calls[1][2]).decode("utf-8")
        snapshot = json.loads(snapshot_text)
        self.assertEqual(snapshot["format_version"], 1)
        self.assertEqual(len(snapshot["students"]), 2)
        self.assertEqual(len(snapshot["enrollments"]), 2)
        self.assertEqual(len(snapshot["assignments"]), 1)
        self.assertNotIn("@", snapshot_text)
        self.assertNotIn("email", snapshot_text.lower())
        inserted = next(
            entry for entry in log
            if entry["table"] == "archived_classes" and entry["op"] == "insert"
        )
        self.assertEqual(inserted["rows"]["storage_path"], path)
        self.assertEqual(inserted["rows"]["snapshot_path"], snapshot_path)
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

    def test_spent_budget_before_upload_leaves_the_class_and_storage_untouched(self):
        response, log, bucket, deleted = self._post(monotonic=[0, 201])
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["error"], self.CHANGED)
        self.assertEqual(bucket.calls, [])
        self.assertEqual(deleted, [])
        self.assertFalse(any(entry["op"] == "insert" for entry in log))

    def test_snapshot_build_error_uploads_nothing(self):
        from app.helpers.archive_snapshot import SnapshotCountError
        failed, _log, bucket, deleted = self._post(snapshot_error=RuntimeError("snapshot failed"))
        self.assertEqual(failed.status_code, 500)
        self.assertEqual(
            failed.get_json()["error"],
            "Could not build the archive. Nothing was changed.",
        )
        self.assertEqual(bucket.calls, [])
        self.assertEqual(deleted, [])
        mismatched, _log, mismatch_bucket, _deleted = self._post(
            snapshot_error=SnapshotCountError("enrollments"),
        )
        self.assertEqual(mismatched.status_code, 500)
        self.assertEqual(
            mismatched.get_json()["error"],
            "Could not build the archive. Nothing was changed.",
        )
        self.assertEqual(mismatch_bucket.calls, [])

    def test_snapshot_upload_failure_removes_both_objects(self):
        response, log, bucket, deleted = self._post(bucket_fail="snapshot-upload")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.get_json()["error"],
            "Could not save the archive. The class was not changed.",
        )
        self.assertEqual(
            [call[0] for call in bucket.calls],
            ["upload", "upload", "remove", "remove"],
        )
        self.assertEqual(
            bucket.calls[-2][1],
            [f"inst1/{self.ARCHIVE_ID}.snapshot.json.gz"],
        )
        self.assertEqual(bucket.calls[-1][1], [f"inst1/{self.ARCHIVE_ID}.zip"])
        self.assertFalse(any(entry["op"] == "insert" for entry in log))
        self.assertEqual(deleted, [])

    def test_snapshot_download_mismatch_removes_both_objects(self):
        response, log, bucket, deleted = self._post(bucket_fail="snapshot-download")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.get_json()["error"],
            "The saved archive did not verify and was removed. The class was not changed.",
        )
        self.assertEqual(bucket.calls[-1][0], "remove")
        self.assertEqual(bucket.calls[-1][1], [f"inst1/{self.ARCHIVE_ID}.zip"])
        self.assertEqual(
            bucket.calls[-2][1],
            [f"inst1/{self.ARCHIVE_ID}.snapshot.json.gz"],
        )
        self.assertFalse(any(entry["op"] == "insert" for entry in log))
        self.assertEqual(deleted, [])

    def test_each_upload_gets_its_own_options_dict(self):
        from app.helpers.archive_storage import upload_archive
        import app.helpers.archive_storage as storage
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


class TestRestoreChunks(unittest.TestCase):
    def test_row_cap_byte_cap_and_one_huge_record(self):
        import json
        from app.helpers.class_archive import _RESTORE_BYTE_CAP, _restore_chunks

        small = [{"n": index} for index in range(401)]
        sized = _restore_chunks(small)
        self.assertEqual([len(chunk) for chunk in sized], [400, 1])

        wide = [{"blob": "x" * 87370} for _index in range(3)]
        together = json.dumps(wide, ensure_ascii=False).encode("utf-8")
        self.assertGreater(len(together), _RESTORE_BYTE_CAP)
        split = _restore_chunks(wide)
        self.assertGreater(len(split), 1)
        for chunk in split:
            encoded = json.dumps(chunk, ensure_ascii=False).encode("utf-8")
            self.assertLessEqual(len(encoded), _RESTORE_BYTE_CAP)

        with self.assertRaises(ValueError):
            _restore_chunks([{"blob": "x" * _RESTORE_BYTE_CAP}])


class _SnapshotResponse:
    def __init__(self, data, count):
        self.data = data
        self.count = count


class _SnapshotQuery:
    def __init__(self, rows, admin, name):
        self.rows = list(rows)
        self.admin = admin
        self.name = name
        self.filters = []
        self.orders = []
        self.count_mode = None
        self.head = False
        self.range_bounds = None

    def select(self, *args, **kwargs):
        self.admin.selects.append((self.name, args, kwargs))
        self.count_mode = kwargs.get("count")
        self.head = bool(kwargs.get("head"))
        return self

    def eq(self, column, value):
        self.filters.append(("eq", column, value))
        return self

    def in_(self, column, values):
        copied = list(values)
        self.admin.in_calls.append((column, copied))
        self.filters.append(("in", column, copied))
        return self

    def or_(self, expr):
        self.admin.or_calls.append(expr)
        self.filters.append(("or", expr))
        return self

    def order(self, column, desc=False):
        self.orders.append((column, desc))
        self.admin.orders.append((self.name, column))
        return self

    def range(self, start, end):
        self.range_bounds = (start, end)
        self.admin.ranges.append((self.name, start, end))
        return self

    def _or_matches(self, row, expr):
        import re
        clauses = re.findall(r"([A-Za-z0-9_]+)\.in\.\(([^)]*)\)", expr)
        for column, raw in clauses:
            ids = [item for item in raw.split(",") if item]
            if str(row.get(column) or "") in ids:
                return True
        return False

    def _filtered(self):
        kept = []
        for row in self.rows:
            matched = True
            for kind, column, value in (
                (item[0], item[1], item[2] if len(item) > 2 else None) for item in self.filters
            ):
                if kind == "eq" and str(row.get(column)) != str(value):
                    matched = False
                elif kind == "in" and str(row.get(column) or "") not in {str(item) for item in value}:
                    matched = False
                elif kind == "or" and not self._or_matches(row, column):
                    matched = False
            if matched:
                kept.append(row)
        for column, desc in reversed(self.orders):
            kept.sort(key=lambda item: str(item.get(column) or ""), reverse=desc)
        return kept

    def execute(self):
        rows = self._filtered()
        if self.head:
            count = len(rows)
            if self.admin.short:
                count = max(count - 1, 0)
            return _SnapshotResponse([], count)
        if self.range_bounds is not None:
            start, end = self.range_bounds
            rows = rows[start:end + 1]
        count = len(rows) if self.count_mode else None
        return _SnapshotResponse(rows, count)


class _SnapshotAdmin:
    def __init__(self, tables, short=False):
        self.table_names = []
        self.rows_for = tables
        self.short = short
        self.selects = []
        self.in_calls = []
        self.or_calls = []
        self.orders = []
        self.ranges = []

    def table(self, name):
        self.table_names.append(name)
        return _SnapshotQuery(self.rows_for.get(name, []), self, name)


class TestArchiveSnapshot(unittest.TestCase):
    def _tables(self, grade_count=1):
        grades = [
            {
                "id": "g%s" % index,
                "student_id": "s1",
                "learning_objective_id": "lo1",
                "assignment_id": "a1",
                "top_score": "M",
                "second_score": None,
                "counts_for_mastery": True,
            }
            for index in range(grade_count)
        ]
        return {
            "classes": [{
                "id": "c1",
                "name": "Algebra",
                "instructor_id": "inst1",
                "days": "MW",
                "section_number": "01",
            }],
            "learning_objectives": [{
                "id": "lo1",
                "class_id": "c1",
                "name": "Factor",
                "description": "Factor expressions",
            }],
            "assignments": [{
                "id": "a1",
                "class_id": "c1",
                "name": "Quiz",
                "homework_group": "Exam",
                "date_returned": "2026-08-20",
                "revision_due": None,
                "assignment_type": "exam",
            }],
            "assignment_objectives": [{
                "assignment_id": "a1",
                "learning_objective_id": "lo1",
            }],
            "enrollments": [{
                "id": "e1",
                "class_id": "c1",
                "student_id": "s1",
                "muted": False,
            }],
            "grades": grades,
            "homework_scores": [],
            "free_passes": [],
            "profiles": [{
                "id": "s1",
                "full_name": "Lee, Ana",
                "email": "ana@example.com",
            }],
        }

    def _assignments(self, count):
        return [
            {
                "id": "a%s" % index,
                "class_id": "c1",
                "name": "A%s" % index,
                "homework_group": "Exam",
                "date_returned": "2026-08-20",
                "revision_due": None,
                "assignment_type": "exam",
            }
            for index in range(count)
        ]

    def _build(self, tables=None, students=None, assignments=None, short=False):
        import app.helpers.archive_snapshot as snapshot
        tables = self._tables() if tables is None else tables
        if students is None:
            students = len(tables["enrollments"])
        if assignments is None:
            assignments = len(tables["assignments"])
        admin = _SnapshotAdmin(tables, short=short)
        manifest = {
            "source_students": students,
            "source_assignments": assignments,
        }
        with unittest.mock.patch.object(snapshot, "supabase_admin", admin):
            payload = snapshot.build_class_snapshot("c1", manifest)
        return payload, admin

    def test_snapshot_keeps_names_pages_grades_and_drops_email(self):
        import gzip
        import json
        payload, admin = self._build(self._tables(grade_count=1001))
        document = json.loads(gzip.decompress(payload).decode("utf-8"))
        text = gzip.decompress(payload).decode("utf-8")
        self.assertEqual(document["format_version"], 1)
        self.assertEqual(document["students"], [{"full_name": "Lee, Ana", "id": "s1"}])
        self.assertEqual(len(document["grades"]), 1001)
        self.assertNotIn("@", text)
        self.assertNotIn("email", text.lower())
        self.assertIn(
            ("profiles", ("id, full_name",)),
            [(name, args) for name, args, _kwargs in admin.selects],
        )
        self.assertNotIn("grade_change_log", admin.table_names)
        self.assertNotIn("revision_reminders_sent", admin.table_names)
        self.assertEqual(
            [column for table, column in admin.orders if table == "assignment_objectives"],
            [
                "assignment_id", "learning_objective_id",
                "assignment_id", "learning_objective_id",
            ],
        )
        self.assertIn(("grades", "id"), admin.orders)
        self.assertIn(("classes", "id"), admin.orders)
        grade_ranges = [(start, end) for table, start, end in admin.ranges if table == "grades"]
        self.assertIn((0, 999), grade_ranges)
        self.assertIn((1000, 1999), grade_ranges)

    def test_enrollment_count_must_match_the_manifest(self):
        from app.helpers.archive_snapshot import SnapshotCountError
        with self.assertRaises(SnapshotCountError):
            self._build(self._tables(), students=9)

    def test_a_short_head_count_raises(self):
        from app.helpers.archive_snapshot import SnapshotCountError
        with self.assertRaises(SnapshotCountError):
            self._build(self._tables(), short=True)

    def test_fifty_one_assignment_ids_are_chunked_and_each_grade_is_kept_once(self):
        import gzip
        import json
        tables = self._tables(grade_count=0)
        tables["assignments"] = self._assignments(51)
        tables["grades"] = [
            {
                "id": "g%s" % index,
                "student_id": "s1",
                "learning_objective_id": "lo1",
                "assignment_id": "a%s" % index,
                "top_score": "M",
                "second_score": None,
                "counts_for_mastery": True,
            }
            for index in range(51)
        ]
        payload, admin = self._build(tables, students=1, assignments=51)
        document = json.loads(gzip.decompress(payload).decode("utf-8"))
        grade_ids = [row["id"] for row in document["grades"]]
        self.assertEqual(admin.or_calls, [])
        self.assertEqual(len(grade_ids), 51)
        self.assertEqual(len(set(grade_ids)), 51)

    def test_a_grade_returned_by_both_chunks_appears_once(self):
        import gzip
        import json
        tables = self._tables(grade_count=0)
        tables["assignments"] = self._assignments(51)
        tables["grades"] = [
            {
                "id": "both",
                "student_id": "s1",
                "learning_objective_id": "lo1",
                "assignment_id": "a0",
                "top_score": "M",
                "second_score": None,
                "counts_for_mastery": True,
            },
            {
                "id": "assignment-only",
                "student_id": "s1",
                "learning_objective_id": "lo-other",
                "assignment_id": "a1",
                "top_score": "M",
                "second_score": None,
                "counts_for_mastery": True,
            },
        ]
        payload, admin = self._build(tables, students=1, assignments=51)
        document = json.loads(gzip.decompress(payload).decode("utf-8"))
        ids = [row["id"] for row in document["grades"]]
        self.assertEqual(admin.or_calls, [])
        self.assertTrue(any(column == "assignment_id" for column, _values in admin.in_calls))
        self.assertIn(("learning_objective_id", ["lo1"]), admin.in_calls)
        self.assertEqual(sorted(ids), ["assignment-only", "both"])
        self.assertEqual(len(ids), 2)

    def test_punctuation_in_ids_stays_inside_one_in_list(self):
        import gzip
        import json
        tables = self._tables(grade_count=0)
        tables["assignments"] = [{
            "id": "a,1)",
            "class_id": "c1",
            "name": "Quiz",
            "homework_group": "Exam",
            "date_returned": "2026-08-20",
            "revision_due": None,
            "assignment_type": "exam",
        }]
        tables["learning_objectives"] = [{
            "id": 'lo"1',
            "class_id": "c1",
            "name": "Factor",
            "description": "Factor expressions",
        }]
        tables["assignment_objectives"] = [{
            "assignment_id": "a,1)",
            "learning_objective_id": 'lo"1',
        }]
        tables["grades"] = [
            {
                "id": "kept",
                "student_id": "s1",
                "learning_objective_id": 'lo"1',
                "assignment_id": "a,1)",
                "top_score": "M",
                "second_score": None,
                "counts_for_mastery": True,
            },
            {
                "id": "decoy",
                "student_id": "s1",
                "learning_objective_id": "lo-other",
                "assignment_id": "a",
                "top_score": "R",
                "second_score": None,
                "counts_for_mastery": True,
            },
        ]
        payload, admin = self._build(tables, students=1, assignments=1)
        document = json.loads(gzip.decompress(payload).decode("utf-8"))
        self.assertEqual([row["id"] for row in document["grades"]], ["kept"])
        self.assertEqual(admin.or_calls, [])
        flat = [value for _column, values in admin.in_calls for value in values]
        self.assertIn("a,1)", flat)
        self.assertIn('lo"1', flat)
        self.assertNotIn("a", flat)
        self.assertNotIn("1)", flat)
        for _column, values in admin.in_calls:
            self.assertEqual(len(values), 1)

    def test_one_student_with_two_enrollment_rows_is_one_student(self):
        import gzip
        import json
        tables = self._tables()
        tables["enrollments"] = [
            {"id": "e1", "class_id": "c1", "student_id": "s1", "muted": False},
            {"id": "e2", "class_id": "c1", "student_id": "s1", "muted": True},
        ]
        payload, _admin = self._build(tables, students=2, assignments=1)
        document = json.loads(gzip.decompress(payload).decode("utf-8"))
        self.assertEqual(len(document["enrollments"]), 2)
        self.assertEqual(document["students"], [{"full_name": "Lee, Ana", "id": "s1"}])

    def test_a_key_whose_name_contains_email_is_rejected(self):
        from app.helpers.archive_snapshot import SnapshotCountError
        tables = self._tables()
        tables["classes"][0]["student_email"] = "hidden"
        with self.assertRaises(SnapshotCountError):
            self._build(tables)


class TestPublicBaseUrlRequired(unittest.TestCase):
    def test_production_boot_requires_public_base_url(self):
        import importlib
        import config
        env = {
            "APP_ENV": "production",
            "SECRET_KEY": "test-secret",
            "CORS_ALLOWED_ORIGINS": "https://clarity.example",
            "PUBLIC_BASE_URL": "",
            "RAILWAY_ENVIRONMENT_ID": "",
            "RAILWAY_PROJECT_ID": "",
            "RAILWAY_SERVICE_ID": "",
        }
        try:
            with unittest.mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaises(RuntimeError) as caught:
                    importlib.reload(config)
            self.assertIn("PUBLIC_BASE_URL", str(caught.exception))
        finally:
            importlib.reload(config)
            import app.helpers.account_mail as account_mail
            account_mail.Config = config.Config


class TestCsvUploadMarkup(unittest.TestCase):
    def _read(self, name):
        path = os.path.join(os.path.dirname(__file__), "..", "app", "templates", "classes", name)
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
        src = self._read("app", "templates", "classes", "class_assignments.html")
        tag = self._opening_tag(src, "loCsvPreviewModal")
        self.assertNotIn("onclick", tag.lower())

    def test_assignments_calls_shared_scroll_helpers(self):
        src = self._read("app", "templates", "classes", "class_assignments.html")
        self.assertIn("ClarityUI.lockPageScroll()", src)
        self.assertIn("ClarityUI.unlockPageScroll()", src)

    def test_ui_helpers_defines_and_exports_scroll_helpers(self):
        src = self._read("app", "static", "js", "ui_helpers.js")
        self.assertIn("function lockPageScroll()", src)
        self.assertIn("function unlockPageScroll()", src)
        self.assertIn("lockPageScroll,", src)
        self.assertIn("unlockPageScroll,", src)

    def test_student_upload_overlays_have_no_onclick(self):
        src = self._read("app", "templates", "classes", "class_detail.html")
        for element_id in ("uploadStudentsModal", "studentUploadPreviewModal"):
            tag = self._opening_tag(src, element_id)
            self.assertNotIn("onclick", tag.lower())
        self.assertIn("ClarityUI.lockPageScroll()", src)
        self.assertIn("ClarityUI.unlockPageScroll()", src)
        self.assertNotIn("setPageScrollLocked", src)

    def test_class_detail_lo_csv_preview_code_is_gone(self):
        src = self._read("app", "templates", "classes", "class_detail.html")
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
            src = self._read("app", "templates", "classes", name)
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
            os.path.dirname(__file__), "..", "app", "templates", "classes", name,
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
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "app", "static", "js",
            "class_learning_objectives_summary.js",
        )
        with open(script_path, encoding="utf-8") as fh:
            src = fh.read() + "\n" + src
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
                "classes/class_learning_objectives_summary.html",
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
                "classes/class_students.html",
                class_id="c1",
                class_name="MW",
                students=[student],
            )
        self.assertIn("/class/c1/learning-objectives?print=1", html)
        self.assertIn("print student sheets", html)


if __name__ == '__main__':
    unittest.main()
