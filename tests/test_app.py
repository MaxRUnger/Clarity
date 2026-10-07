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
    """Homework % columns on scanned sheets are not learning objectives."""

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
        self.assertIn("gradesheet format", err)

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
        self.assertIn("pdf_analyzer.js') }}?v=19", src)

    def test_pdf_analyzer_posts_ignored_names_without_preview(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "static", "js", "pdf_analyzer.js",
        )
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("preview-import-name-matches", src)
        self.assertNotIn("name_resolutions", src)
        self.assertIn("ignored_names", src)

    def test_add_student_flag_inputs_are_not_native_constraints(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "static", "js", "pdf_analyzer.js",
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

    def test_pdf_analyzer_has_no_add_student_notices(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "static", "js", "pdf_analyzer.js",
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

    def test_valid_csv_does_not_call_gemini_and_can_match_assignment(self):
        from app import routes as r
        gemini = unittest.mock.MagicMock()
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
                ), \
                unittest.mock.patch.object(r, "get_gemini_analyzer", gemini):
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
        gemini.assert_not_called()

    def test_three_metadata_row_csv_parses_header_and_students(self):
        from app import routes as r
        gemini = unittest.mock.MagicMock()
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
                ), \
                unittest.mock.patch.object(r, "get_gemini_analyzer", gemini):
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
        gemini.assert_not_called()

    def test_malformed_csv_400_without_gemini(self):
        from app import routes as r
        gemini = unittest.mock.MagicMock()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(
                    r.Course, "get_learning_objectives", return_value=[{"vendor_code": "D1"}]
                ), \
                unittest.mock.patch.object(r, "get_gemini_analyzer", gemini):
            rv = self.client.post(
                "/api/class/c1/parse-grade-csv",
                data={"file": (io.BytesIO(b"nope"), "bad.csv")},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 400)
        gemini.assert_not_called()


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

    def test_pdf_analyzer_pushes_the_stored_name(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "static", "js", "pdf_analyzer.js",
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
        return self._render(
            "class_detail.html",
            class_id="c1",
            class_name="Test Upload",
            class_days="MW",
            class_section=section,
            students=[],
            all_students=[],
            learning_objectives=[],
            overdue_revisions=[],
            assignments=[],
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

    def test_history_email_and_report_print_keep_raw_name(self):
        history = self._render(
            "student_history.html",
            class_id="c1",
            student_id="s1",
            student_name="Lee, Ana",
            student_email="a@b.edu",
            instructor_title="Professor Estes",
            learning_objectives=[],
            class_name="Test Upload",
            class_days="MW",
            class_section="F26",
        )
        self.assertIn("Test Upload | MW | F26", history)
        self.assertIn('"class_name": "Test Upload"', history)

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
    def __init__(self, log, table, rows, fail_delete):
        self.log = log
        self.table = table
        self.rows = list(rows)
        self.fail_delete = fail_delete
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

    def eq(self, column, value):
        self.filters.append(("eq", column, value))
        return self

    def in_(self, column, values):
        self.filters.append(("in", column, list(values)))
        return self

    def order(self, *args, **kwargs):
        return self

    def range(self, start, end):
        return self

    def execute(self):
        if self.op == "delete" and self.fail_delete == self.table:
            self.log.append({
                "table": self.table,
                "op": "delete-failed",
                "filters": list(self.filters),
            })
            raise RuntimeError("delete failed")
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
        from flask import render_template, session
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
                overdue_revisions=[],
                assignments=[],
            )
        self.assertIn("Manage Students", html)
        self.assertIn('id="activeStudentsCount"', html)
        match = re.search(r'id="activeStudentsCount"[^>]*>([^<]*)<', html)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1).strip(), "1")


if __name__ == '__main__':
    unittest.main()
