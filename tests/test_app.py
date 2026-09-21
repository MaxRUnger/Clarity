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
    _student_row_sort_key,
    _student_sort_key_last_name,
    _generate_sort_name,
    _format_name_last_first,
    _student_display_name,
    _aggregate_lo_grades,
    parse_blank_gradesheet_csv_text,
    parse_students_csv_text,
    _csv_format_hw_score,
    _gradesheet_csv_data_rows,
    _gradesheet_letter_map_from_rows,
    _build_gradesheet_csv_text,
    _BLANK_GRADESHEET_NOTE,
    _enrolled_import_name_index,
    _lookup_enrolled_import_student_id,
    _is_same_instructor_name_candidate,
    _first_seen_unique_sheet_names,
    _list_same_instructor_other_class_enrollments,
    _collect_preview_import_name_matches,
    _plan_import_name_resolutions,
    MAX_IMPORT_ROWS,
)
from app import create_app


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


class TestGenerateSortName(unittest.TestCase):
    """_generate_sort_name: the heuristic that converts 'First Last[...]' → 'Last[...], First'."""

    def test_simple_two_token(self):
        self.assertEqual(_generate_sort_name("Cali Smith"), "Smith, Cali")

    def test_multi_word_last_name(self):
        """Everything after the first token is the last-name cluster."""
        self.assertEqual(_generate_sort_name("Evelyn Juarez Salgado"), "Juarez Salgado, Evelyn")

    def test_von_prefix(self):
        self.assertEqual(_generate_sort_name("Zachary Von Huben"), "Von Huben, Zachary")

    def test_suffix_kept_in_cluster(self):
        """'Jr' is part of the last-name cluster, not stripped."""
        self.assertEqual(_generate_sort_name("Emilio Benito Velasco Jr"), "Benito Velasco Jr, Emilio")

    def test_hyphenated_last_name(self):
        self.assertEqual(_generate_sort_name("Kaiya Smith-Pauley"), "Smith-Pauley, Kaiya")

    def test_hyphen_with_trailing_space_preserved(self):
        """Space-after-hyphen from Canvas import is preserved exactly."""
        self.assertEqual(_generate_sort_name("Kaiya Smith- Pauley"), "Smith- Pauley, Kaiya")

    def test_single_token_unchanged(self):
        self.assertEqual(_generate_sort_name("Madonna"), "Madonna")

    def test_comma_format_passthrough(self):
        """Already-'Last, First' strings are returned unchanged."""
        self.assertEqual(_generate_sort_name("Smith, Cali"), "Smith, Cali")

    def test_empty_string(self):
        self.assertEqual(_generate_sort_name(""), "")

    def test_zetina_ariza(self):
        self.assertEqual(_generate_sort_name("Andrew Zetina Ariza"), "Zetina Ariza, Andrew")


class TestStudentSortKeyLastName(unittest.TestCase):
    """Students lists sort by family name via UCA (pyuca), matching Canvas ordering."""

    def test_simple_last_name_order(self):
        """Adams sorts before Zenith."""
        rows = [{"full_name": "Bob Zenith"}, {"full_name": "Zoe Adams"}]
        ordered = sorted(rows, key=_student_row_sort_key)
        self.assertEqual([r["full_name"] for r in ordered], ["Zoe Adams", "Bob Zenith"])

    def test_comma_format_sorts_correctly(self):
        """'Washington, George' sorts before 'Zenith, Bob'."""
        names = ["Zenith, Bob", "Washington, George"]
        ordered = sorted(names, key=_student_sort_key_last_name)
        self.assertEqual(ordered, ["Washington, George", "Zenith, Bob"])

    def test_organize_buckets_sorted_by_last_name(self):
        students = [
            {
                "id": "1",
                "full_name": "Bob Zenith",
                "grades": [
                    {"learning_objective_id": "10", "top_score": "M", "second_score": "M"},
                ],
            },
            {
                "id": "2",
                "full_name": "Zoe Adams",
                "grades": [
                    {"learning_objective_id": "10", "top_score": "M", "second_score": "M"},
                ],
            },
        ]
        los = [{"id": "10", "name": "LO-A"}]
        result = organize_by_learning_objectives(students, los)
        names = [s["name"] for s in result[0]["students_with_2m"]]
        self.assertEqual(names, ["Adams Zoe", "Zenith Bob"])

    def test_sort_name_in_dict_takes_precedence(self):
        """When sort_name is in the dict, _student_row_sort_key uses it directly."""
        rows = [
            # sort_name says "Z" cluster — should sort last despite full_name starting with A
            {"full_name": "Alice Apple", "sort_name": "Zenith, Alice"},
            {"full_name": "Bob Banana", "sort_name": "Adams, Bob"},
        ]
        ordered = sorted(rows, key=_student_row_sort_key)
        self.assertEqual(ordered[0]["full_name"], "Bob Banana")   # Adams → first
        self.assertEqual(ordered[1]["full_name"], "Alice Apple")  # Zenith → last


class TestCanvasRosterSort(unittest.TestCase):
    """UCA sort matches Canvas roster ordering exactly for a real 38-student class.

    Canvas exports 'Last, First' strings that we store as 'First Last'.
    _generate_sort_name recovers the original Canvas string, and pyuca on it
    reproduces Canvas ordering exactly — verified against a real export.

    Key edge cases confirmed:
    - Smith- Pauley, Kaiya (space-after-hyphen) sorts BEFORE Smith, Cali/Parker
    - Von Huben, Zachary (Von prefix) sorts under 'V'
    - Juarez Salgado, Evelyn (multi-word last) sorts under 'J'
    - Benito Velasco Jr, Emilio (suffix cluster) sorts under 'B'
    - Zetina Ariza, Andrew (multi-word last) sorts under 'Z'
    """

    # Ground-truth Canvas order (38 students, real export)
    CANVAS_ORDER = [
        "Abdulkadir, Abdulwahid", "Alesna, Jaiden", "Alonso, Ivan",
        "Beckham, Jake", "Benito Velasco Jr, Emilio", "Benjamin, Savannah",
        "Berger, Mckinley", "Boe, Aryanna", "Cisneros, Dylan",
        "Cotsidas, Caris", "Cotton, Marcus", "Gamero, David",
        "Guzman, Cesar", "Heredia, Roger", "Hernandez, Celeste",
        "Howard, Caleb", "Javien, Jennilyn", "Juarez Salgado, Evelyn",
        "Keegan, Joshua", "Melssen, Zachary", "Mendoza, Ernesto",
        "Nazimi, Subhan", "Ortega, Armando", "Prych, Joshua",
        "Redmond, Franki", "Roberts, Rider", "Rodriguez, Maya",
        "Romero, Reyna", "Smith- Pauley, Kaiya", "Smith, Cali",
        "Smith, Parker", "Spicka, Kyle", "Taylor, Hunter",
        "Trejo, Esperanza", "Von Huben, Zachary", "Weidenthaler, Carl",
        "Xie, Xun", "Zetina Ariza, Andrew",
    ]

    @staticmethod
    def _canvas_to_stored(canvas_name: str) -> str:
        """Convert 'Last, First' Canvas export to stored 'First Last' format."""
        if "," in canvas_name:
            parts = canvas_name.split(",", 1)
            return parts[1].strip() + " " + parts[0].strip()
        return canvas_name

    def test_38_roster_exact_canvas_order(self):
        """Sorting stored names by _student_sort_key_last_name gives EXACT Canvas order."""
        stored_names = [self._canvas_to_stored(n) for n in self.CANVAS_ORDER]
        # Shuffle to ensure sorting actually works
        import random
        shuffled = list(stored_names)
        random.shuffle(shuffled)
        sorted_names = sorted(shuffled, key=_student_sort_key_last_name)
        self.assertEqual(sorted_names, stored_names,
                         "Sort order does not match Canvas roster. Mismatches:\n" +
                         "\n".join(f"  pos {i+1}: got {sorted_names[i]!r}, want {stored_names[i]!r}"
                                   for i in range(len(sorted_names))
                                   if sorted_names[i] != stored_names[i]))

    def test_smith_cluster_order_matches_canvas(self):
        """Canvas puts Smith- Pauley (Kaiya) BEFORE Smith, Cali and Smith, Parker."""
        # Stored as imported from Canvas (space after hyphen preserved)
        three = [
            self._canvas_to_stored("Smith, Cali"),       # "Cali Smith"
            self._canvas_to_stored("Smith, Parker"),      # "Parker Smith"
            self._canvas_to_stored("Smith- Pauley, Kaiya"),  # "Kaiya Smith- Pauley"
        ]
        ordered = sorted(three, key=_student_sort_key_last_name)
        # Canvas order: Kaiya first, then Cali, then Parker
        self.assertEqual(ordered[0], "Kaiya Smith- Pauley")
        self.assertEqual(ordered[1], "Cali Smith")
        self.assertEqual(ordered[2], "Parker Smith")

    def test_generate_sort_name_38_names_all_correct(self):
        """_generate_sort_name regenerates all 38 Canvas sort_name strings exactly."""
        wrong = []
        for canvas_name in self.CANVAS_ORDER:
            stored = self._canvas_to_stored(canvas_name)
            generated = _generate_sort_name(stored)
            if generated != canvas_name:
                wrong.append(f"  Canvas={canvas_name!r}, stored={stored!r}, generated={generated!r}")
        self.assertEqual(wrong, [], "Heuristic mismatch for:\n" + "\n".join(wrong))

    def test_csv_import_preview_uses_uca(self):
        """parse_students_csv_text applies UCA sort matching Canvas cluster ordering."""
        # Mix up the three Smiths + one extra to verify stable-ish ordering
        csv_text = "name\nParker Smith\nKaiya Smith-Pauley\nCali Smith\nZoe Adams\n"
        rows, warnings = parse_students_csv_text(csv_text)
        self.assertEqual(warnings, [])
        names = [r["full_name"] for r in rows]
        # Adams first, then Smith cluster. Within Smiths, hyphen-with-no-space
        # "Smith-Pauley" sorts differently from Canvas's space version, but is
        # still grouped with Smiths and before Zoe Adams is impossible.
        self.assertEqual(names[0], "Zoe Adams")
        # All three Smiths must appear after Adams
        self.assertIn("Cali Smith", names[1:])
        self.assertIn("Parker Smith", names[1:])
        self.assertIn("Kaiya Smith-Pauley", names[1:])

    def test_student_row_sort_key_uses_stored_sort_name(self):
        """When sort_name is present it overrides on-the-fly generation."""
        # Simulate a student whose sort_name was manually corrected
        rows = [
            {"full_name": "Emilio Benito Velasco Jr", "sort_name": "Benito Velasco Jr, Emilio"},
            {"full_name": "Zoe Adams", "sort_name": "Adams, Zoe"},
        ]
        ordered = sorted(rows, key=_student_row_sort_key)
        self.assertEqual(ordered[0]["full_name"], "Zoe Adams")
        self.assertEqual(ordered[1]["full_name"], "Emilio Benito Velasco Jr")

    def test_regression_non_hyphenated_alphabetical(self):
        """Regression: plain non-hyphenated names sort alphabetically by last name."""
        rows = [{"full_name": "Bob Zenith"}, {"full_name": "Zoe Adams"}]
        result = [r["full_name"] for r in sorted(rows, key=_student_row_sort_key)]
        self.assertEqual(result, ["Zoe Adams", "Bob Zenith"])


class TestFormatNameLastFirst(unittest.TestCase):
    """Roster display: family name first."""

    def test_first_last_to_last_first(self):
        self.assertEqual(_format_name_last_first("John Smith"), "Smith John")

    def test_comma_form(self):
        self.assertEqual(_format_name_last_first("Washington, George"), "Washington George")

    def test_student_display_name_formats(self):
        self.assertEqual(
            _student_display_name({"id": "x", "full_name": "John Smith"}),
            "Smith John",
        )

    def test_student_display_name_prefers_comma_sort_name(self):
        cases = [
            ("Kaiya Smith- Pauley", "Smith- Pauley, Kaiya", "Smith- Pauley Kaiya"),
            ("Zachary Von Huben", "Von Huben, Zachary", "Von Huben Zachary"),
            ("Evelyn Juarez Salgado", "Juarez Salgado, Evelyn", "Juarez Salgado Evelyn"),
            ("Andrew Zetina Ariza", "Zetina Ariza, Andrew", "Zetina Ariza Andrew"),
            ("Emilio Benito Velasco Jr", "Benito Velasco Jr, Emilio", "Benito Velasco Jr Emilio"),
            ("Jaiden Alesna", "Alesna, Jaiden", "Alesna Jaiden"),
        ]
        for full_name, sort_name, expected in cases:
            self.assertEqual(
                _student_display_name({
                    "id": "x",
                    "full_name": full_name,
                    "sort_name": sort_name,
                }),
                expected,
                msg=full_name,
            )

    def test_student_display_name_empty_sort_name_falls_back(self):
        self.assertEqual(
            _student_display_name({
                "id": "x",
                "full_name": "John Smith",
                "sort_name": "",
            }),
            "Smith John",
        )

    def test_no_comma_suffix_stays_with_preceding_token(self):
        self.assertEqual(
            _format_name_last_first("Emilio Benito Velasco Jr"),
            "Velasco Jr Emilio Benito",
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
        response = self.client.get('/dashboard')
        # Should redirect to login when session has no user_id
        self.assertIn(response.status_code, (302, 404))

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
    lines.append("— Instructor (via Project Clarity)")
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
        )

    def _get_assignment_grades(self, assignment_id, *, owns_class=True, belongs=True):
        patches = self._grades_patches(owns_class=owns_class, belongs=belongs)
        with patches[0], patches[1], patches[2], patches[3]:
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
        send_mock.assert_called_once_with(STU_ALEX_EMAIL, subject, body)
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
        send_mock.assert_called_once_with(STU_ALEX_EMAIL, subject, body)
        sent_body = send_mock.call_args[0][2]
        self.assertIn("Not graded", sent_body)
        self.assertNotIn("D1  |  M", sent_body)

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


class TestGradesheetExportPrefill(unittest.TestCase):
    def test_csv_format_hw_score(self):
        self.assertEqual(_csv_format_hw_score(None), "")
        self.assertEqual(_csv_format_hw_score(85), "85")
        self.assertEqual(_csv_format_hw_score(85.0), "85")
        self.assertEqual(_csv_format_hw_score(-1), "-1")

    def test_data_rows_prefill_hw_and_letters_leave_true_gaps_blank(self):
        students = [
            {"id": "stu-1", "name": "Doe Jane"},
            {"id": "stu-2", "name": "Smith Alex"},
        ]
        los = [
            {"id": "lo-d1", "vendor_code": "D1"},
            {"id": "lo-d2", "vendor_code": "D2"},
        ]
        hw_map = {"stu-1": 85}
        letter_map = {("stu-1", "lo-d1"): "M"}
        rows = _gradesheet_csv_data_rows(students, los, hw_map, letter_map)
        self.assertEqual(rows[0], ["Doe Jane", "85", "M", ""])
        self.assertEqual(rows[1], ["Smith Alex", "", "", ""])

    def test_letter_map_from_assignment_rows(self):
        mapped = _gradesheet_letter_map_from_rows([
            {"student_id": "stu-1", "learning_objective_id": "lo-d1", "top_score": "M"},
            {"student_id": "stu-1", "learning_objective_id": "lo-d2", "top_score": None},
        ])
        self.assertEqual(mapped[("stu-1", "lo-d1")], "M")
        self.assertNotIn(("stu-1", "lo-d2"), mapped)

    def test_blank_template_csv_has_note_and_empty_grade_cells(self):
        students = [{"id": "stu-1", "name": "Doe Jane"}]
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
        students = [{"id": "stu-1", "name": "Doe Jane"}]
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
                    return_value=([{"id": "stu-1", "name": "Doe Jane"}], [], {}),
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


class TestImportRosterNameMatch(unittest.TestCase):
    def test_last_first_export_name_hits_stored_first_last(self):
        index = _enrolled_import_name_index([
            {"id": "stu-1", "full_name": "Jane Doe"},
        ])
        self.assertEqual(_lookup_enrolled_import_student_id("Doe Jane", index), "stu-1")
        self.assertEqual(_lookup_enrolled_import_student_id("Jane Doe", index), "stu-1")
        self.assertIsNone(_lookup_enrolled_import_student_id("Nobody", index))


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


class TestRemoveStudentAuthDecorator(unittest.TestCase):
    """api_remove_student_from_class must reject non-instructors with 401,
    not 403 — the role check should fire at the decorator level before
    ownership is even evaluated.

    Before fix: @api_login_required let any authenticated session reach
    _instructor_owns_class, which returned 403 (role-blind).
    After fix: @api_instructor_required checks role == 'instructor' and
    returns 401 immediately for student sessions.
    """

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_student_session_gets_401_not_403(self):
        """A logged-in student must be turned away with 401 before ownership runs."""
        from app import routes as r
        with self.client.session_transaction() as sess:
            sess["user_id"] = "student-1"
            sess["role"] = "student"        # not 'instructor'
            sess["csrf_token"] = "test-csrf"

        # _instructor_owns_class must never be called — if it were, it would
        # return False and yield 403, masking the decorator regression.
        with unittest.mock.patch.object(
            r, "_instructor_owns_class"
        ) as mock_owns:
            rv = self.client.post(
                "/api/class/any-class/remove_student",
                json={"student_id": "stu-1"},
                headers={"X-CSRF-Token": "test-csrf"},
            )

        self.assertEqual(rv.status_code, 401)
        mock_owns.assert_not_called()

    def test_unauthenticated_gets_401(self):
        """No session at all must also yield 401."""
        rv = self.client.post(
            "/api/class/any-class/remove_student",
            json={"student_id": "stu-1"},
            headers={"X-CSRF-Token": "test-csrf"},
        )
        self.assertEqual(rv.status_code, 401)

    def test_instructor_session_passes_decorator(self):
        """A valid instructor session reaches the route body (ownership then
        returns 403 from _instructor_owns_class=False, proving the decorator
        itself was satisfied)."""
        from app import routes as r
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst-1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"

        with unittest.mock.patch.object(
            r, "_instructor_owns_class", return_value=False
        ):
            rv = self.client.post(
                "/api/class/any-class/remove_student",
                json={"student_id": "stu-1"},
                headers={"X-CSRF-Token": "test-csrf"},
            )

        # 403 means the decorator passed and _instructor_owns_class ran.
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


class TestGradeChangeLogUpserts(unittest.TestCase):
    """Grade UPSERT operations must write an audit row to grade_change_log.

    Before fix: _log_grade_upserts did not exist — grade_change_log only ever
    received DELETE rows from _log_grade_deletions. Every score written via
    save_grades, api_update_grade, or api_import_grades was invisible in the
    audit log.
    After fix: save_grades calls _log_grade_upserts after a successful upsert,
    producing operation="UPSERT" rows that mirror the rows written to the grades
    table.
    """

    def setUp(self):
        from app import create_app
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def _make_sa_mock(self):
        """Return (sa, grade_rows_captured, log_rows_captured).

        Uses table-name dispatch via side_effect so grades.upsert() and
        grade_change_log.insert() are tracked independently.
        """
        grade_rows = []
        log_rows = []
        exec_m = MagicMock()
        exec_m.data = []

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

    def test_save_grades_writes_upsert_log_row(self):
        """save_grades must insert an operation=UPSERT entry per grade written."""
        from app import routes as r
        sa, grade_rows, log_rows = self._make_sa_mock()

        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["csrf_token"] = "test-csrf"

        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_assignment_belongs_to_class", return_value=True), \
                unittest.mock.patch.object(r, "_class_auto_convert_m_enabled", return_value=False), \
                unittest.mock.patch.object(r, "_enrolled_student_ids_for_class", return_value={"stu-1"}), \
                unittest.mock.patch.object(r.Course, "get_assignment_lo_ids", return_value=["lo-x"]), \
                unittest.mock.patch.object(
                    r.Homework, "get_hw_scores_map_for_assignment", return_value={"stu-1": 80}
                ):
            rv = self.client.post(
                "/api/class/class-1/save-grades",
                json={"assignment_id": "asg-1", "grades": {"stu-1|lo-x": "M"}},
                headers={"X-CSRF-Token": "test-csrf"},
            )

        self.assertEqual(rv.status_code, 200, rv.get_json())
        self.assertEqual(len(grade_rows), 1, "sanity: one grade row must be upserted")
        self.assertEqual(len(log_rows), 1, "exactly one audit row must be written to grade_change_log")
        entry = log_rows[0]
        self.assertEqual(entry["operation"], "UPSERT")
        self.assertEqual(entry["student_id"], "stu-1")
        self.assertEqual(entry["learning_objective_id"], "lo-x")
        self.assertEqual(entry["assignment_id"], "asg-1")
        self.assertEqual(entry["changed_by"], "inst1")
        self.assertIsNone(entry["old_value"])
        self.assertEqual(entry["new_value"]["top_score"], "M")

    def test_save_grades_empty_payload_produces_no_log(self):
        """An empty grades dict must produce no upsert and no audit log row
        — _log_grade_upserts must short-circuit on empty rows."""
        from app import routes as r
        sa, grade_rows, log_rows = self._make_sa_mock()

        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["csrf_token"] = "test-csrf"

        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_assignment_belongs_to_class", return_value=True), \
                unittest.mock.patch.object(r, "_class_auto_convert_m_enabled", return_value=False), \
                unittest.mock.patch.object(r, "_enrolled_student_ids_for_class", return_value={"stu-1"}), \
                unittest.mock.patch.object(r.Course, "get_assignment_lo_ids", return_value=["lo-x"]), \
                unittest.mock.patch.object(
                    r.Homework, "get_hw_scores_map_for_assignment", return_value={}
                ):
            rv = self.client.post(
                "/api/class/class-1/save-grades",
                json={"assignment_id": "asg-1", "grades": {}},
                headers={"X-CSRF-Token": "test-csrf"},
            )

        self.assertEqual(rv.status_code, 200)
        self.assertEqual(grade_rows, [])
        self.assertEqual(log_rows, [], "no grade written → no audit row must appear")


class TestStudentNameHtmlEscaping(unittest.TestCase):
    """onclick attributes using student.name must apply the |e filter so a
    name containing a single quote (e.g. O'Brien) doesn't break the JS string.

    Before fix: {{ student.name }} in onclick='...' left the single quote
    unescaped — deleteStudent('uuid', 'O'Brien') is invalid JS.
    After fix: {{ student.name|e }} produces O&#39;Brien which the browser
    decodes back to O'Brien inside the handler.
    """

    def test_name_with_single_quote_is_html_escaped(self):
        """The Jinja2 |e filter must convert ' to &#39; in onclick attributes."""
        from jinja2 import Environment
        env = Environment(autoescape=True)
        tpl = env.from_string(
            "onclick=\"deleteStudent('{{ sid }}', '{{ name|e }}')\""
        )
        rendered = tpl.render(sid="uuid-1", name="O'Brien")
        self.assertIn("O&#39;Brien", rendered,
                      "single quote must be HTML-escaped in onclick attribute")
        self.assertNotIn("O'Brien", rendered,
                         "raw single quote must not appear — it breaks the JS string")

    def test_name_without_special_chars_is_unchanged(self):
        """Names with no HTML-special characters render identically with |e."""
        from jinja2 import Environment
        env = Environment(autoescape=True)
        tpl = env.from_string("onclick=\"deleteStudent('{{ sid }}', '{{ name|e }}')\"")
        rendered = tpl.render(sid="uuid-1", name="Jane Doe")
        self.assertIn("Jane Doe", rendered)

    def test_double_quote_in_name_is_escaped(self):
        """A name containing a double quote must also be safely escaped."""
        from jinja2 import Environment
        env = Environment(autoescape=True)
        tpl = env.from_string("onclick=\"deleteStudent('{{ sid }}', '{{ name|e }}')\"")
        rendered = tpl.render(sid="uuid-1", name='Say "Hello"')
        self.assertIn("&#34;", rendered)
        self.assertNotIn('"Hello"', rendered)


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

    # ── api_add_student_to_class ──────────────────────────────────────────

    def test_add_student_name_255_accepted(self):
        """Exactly 255-char name must pass the length check."""
        from app import routes as r
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        sa = MagicMock()
        q = MagicMock()
        q.execute.return_value = MagicMock(data=[])
        sa.table.return_value = q
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True):
            rv = self.client.post(
                "/api/class/c1/add_student",
                json={"student_name": "A" * 255},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertNotEqual(rv.status_code, 400, "255-char name must not be length-rejected")

    def test_add_student_name_256_rejected(self):
        """256-char student name must be rejected with 400 before any DB call."""
        from app import routes as r
        sa = MagicMock()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True):
            rv = self.client.post(
                "/api/class/c1/add_student",
                json={"student_name": "A" * 256},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 400)
        sa.table.assert_not_called()

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


class _PreviewTableQuery:
    def __init__(self, name, rows):
        self.name = name
        self.rows = rows
        self._eq = {}
        self._in = {}

    def select(self, *args, **kwargs):
        return self

    def eq(self, field, val):
        self._eq[field] = val
        return self

    def in_(self, field, vals):
        self._in[field] = list(vals)
        return self

    def execute(self):
        rows = list(self.rows)
        if self.name == "classes":
            if "instructor_id" in self._eq:
                rows = [c for c in rows if c.get("instructor_id") == self._eq["instructor_id"]]
            if "id" in self._in:
                allowed = {str(x) for x in self._in["id"]}
                rows = [c for c in rows if str(c.get("id")) in allowed]
        elif self.name == "enrollments":
            if "class_id" in self._in:
                allowed = {str(x) for x in self._in["class_id"]}
                rows = [e for e in rows if str(e.get("class_id")) in allowed]
            if "student_id" in self._in:
                allowed = {str(x) for x in self._in["student_id"]}
                rows = [e for e in rows if str(e.get("student_id")) in allowed]
        return MagicMock(data=rows)


class TestPreviewImportNameMatches(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_fuzzy_john_jon_flags_emily_emma_and_john_jane_do_not(self):
        self.assertTrue(_is_same_instructor_name_candidate(
            "Jon Smith", "John Smith", "Smith, John"
        ))
        self.assertFalse(_is_same_instructor_name_candidate(
            "Emily Chen", "Emma Chen", "Chen, Emma"
        ))
        self.assertFalse(_is_same_instructor_name_candidate(
            "John Smith", "Jane Smith", "Smith, Jane"
        ))

    def test_last_first_sheet_names_exact_and_typo_flag(self):
        self.assertTrue(_is_same_instructor_name_candidate(
            "Smith, John", "John Smith", "Smith, John"
        ))
        self.assertTrue(_is_same_instructor_name_candidate(
            "Smith, Jon", "John Smith", "Smith, John"
        ))

    def test_duplicate_input_names_keep_first_seen_spelling(self):
        names, err = _first_seen_unique_sheet_names(
            ["Jon Smith", "jon smith", "Jon Smith"]
        )
        self.assertIsNone(err)
        self.assertEqual(names, ["Jon Smith"])

    def _other_class_fixture(self):
        classes = [
            {"id": "class-a", "name": "Current", "instructor_id": "inst-1"},
            {"id": "class-b", "name": "Algebra 2", "instructor_id": "inst-1"},
            {"id": "class-c", "name": "Foreign", "instructor_id": "inst-2"},
        ]
        enrollments = [
            {
                "student_id": "same-ok",
                "class_id": "class-b",
                "profiles": {
                    "id": "same-ok",
                    "full_name": "John Smith",
                    "sort_name": "Smith, John",
                },
            },
            {
                "student_id": "foreign-only",
                "class_id": "class-c",
                "profiles": {
                    "id": "foreign-only",
                    "full_name": "John Smith",
                    "sort_name": "Smith, John",
                },
            },
            {
                "student_id": "dual-claimed",
                "class_id": "class-b",
                "profiles": {
                    "id": "dual-claimed",
                    "full_name": "Pat Lee",
                    "sort_name": "Lee, Pat",
                },
            },
            {
                "student_id": "dual-claimed",
                "class_id": "class-c",
                "profiles": {
                    "id": "dual-claimed",
                    "full_name": "Pat Lee",
                    "sort_name": "Lee, Pat",
                },
            },
        ]
        sa = MagicMock()
        sa.table.side_effect = lambda name: _PreviewTableQuery(
            name, classes if name == "classes" else enrollments
        )
        return sa

    def test_list_excludes_foreign_and_dual_enrolled_claimed_ids(self):
        from app import routes as r
        sa = self._other_class_fixture()
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_class_instructor_id", return_value="inst-1"):
            rows = _list_same_instructor_other_class_enrollments("class-a")
        ids = {row["profile_id"] for row in rows}
        self.assertIn("same-ok", ids)
        self.assertNotIn("foreign-only", ids)
        self.assertNotIn("dual-claimed", ids)

    def test_fail_closed_elsewhere_lookup_returns_empty_list(self):
        from app import routes as r
        sa = self._other_class_fixture()
        with unittest.mock.patch.object(r, "supabase_admin", sa), \
                unittest.mock.patch.object(r, "_class_instructor_id", return_value="inst-1"), \
                unittest.mock.patch.object(
                    r, "_profile_ids_with_enrollment_elsewhere",
                    side_effect=lambda pids, class_id: set(pids),
                ):
            rows = _list_same_instructor_other_class_enrollments("class-a")
        self.assertEqual(rows, [])

    def test_this_class_roster_name_is_never_returned(self):
        from app import routes as r
        others = [{
            "profile_id": "p-other",
            "full_name": "John Smith",
            "sort_name": "Smith, John",
            "class_id": "class-b",
            "class_name": "Algebra 2",
        }]
        with unittest.mock.patch.object(
            r, "_this_class_enrolled_import_index",
            return_value=_enrolled_import_name_index(
                [{"id": "p-here", "full_name": "John Smith"}]
            ),
        ), unittest.mock.patch.object(
            r, "_list_same_instructor_other_class_enrollments", return_value=others
        ):
            matches = _collect_preview_import_name_matches(
                "class-a", ["John Smith", "Jon Smith"]
            )
        names = [m["name"] for m in matches]
        self.assertNotIn("John Smith", names)
        self.assertEqual(names, ["Jon Smith"])
        self.assertEqual(matches[0]["key"], "jon smith")
        self.assertEqual(matches[0]["candidates"][0]["profile_id"], "p-other")
        self.assertEqual(set(matches[0]["candidates"][0].keys()), {
            "profile_id", "full_name", "class_name",
        })

    def test_non_owner_forbidden(self):
        from app import routes as r
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "_rate_limit", return_value=True), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=False):
            rv = self.client.post(
                "/api/class/class-a/preview-import-name-matches",
                json={"names": ["Jon Smith"]},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 403)
        self.assertFalse(rv.get_json()["success"])

    def test_duplicate_names_route_returns_one_group(self):
        from app import routes as r
        others = [{
            "profile_id": "p1",
            "full_name": "John Smith",
            "sort_name": "Smith, John",
            "class_id": "class-b",
            "class_name": "Algebra 2",
        }]
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "_rate_limit", return_value=True), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(
                    r, "_this_class_enrolled_import_index", return_value={}
                ), \
                unittest.mock.patch.object(
                    r, "_list_same_instructor_other_class_enrollments", return_value=others
                ):
            rv = self.client.post(
                "/api/class/class-a/preview-import-name-matches",
                json={"names": ["Jon Smith", "jon smith"]},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 200)
        body = rv.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(len(body["matches"]), 1)
        self.assertEqual(body["matches"][0]["name"], "Jon Smith")
        self.assertEqual(body["matches"][0]["key"], "jon smith")
        self.assertEqual(body["name_keys"], ["jon smith", "jon smith"])

    def test_name_keys_parallel_to_submitted_names(self):
        from app import routes as r
        others = [{
            "profile_id": "p1",
            "full_name": "John Smith",
            "sort_name": "Smith, John",
            "class_id": "class-b",
            "class_name": "Algebra 2",
        }]
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch.object(r, "_rate_limit", return_value=True), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(
                    r, "_this_class_enrolled_import_index", return_value={}
                ), \
                unittest.mock.patch.object(
                    r, "_list_same_instructor_other_class_enrollments", return_value=others
                ):
            rv = self.client.post(
                "/api/class/class-a/preview-import-name-matches",
                json={"names": ["Jon Smith", "", "Ada Lovelace"]},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 200)
        body = rv.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(len(body["name_keys"]), 3)
        self.assertEqual(body["name_keys"], ["jon smith", "", "ada lovelace"])
        self.assertEqual(len(body["matches"]), 1)
        self.assertEqual(body["matches"][0]["key"], "jon smith")


class TestPlanImportNameResolutions(unittest.TestCase):
    POOL = [
        {
            "profile_id": "p-john",
            "full_name": "John Smith",
            "sort_name": "Smith, John",
            "class_id": "class-b",
            "class_name": "Algebra 2",
        },
        {
            "profile_id": "p-ada",
            "full_name": "Ada Lovelace",
            "sort_name": "Lovelace, Ada",
            "class_id": "class-b",
            "class_name": "Algebra 2",
        },
    ]

    def test_tampered_attach_unknown_profile_id_is_invalid(self):
        result = _plan_import_name_resolutions(
            ["Jon Smith"],
            {},
            self.POOL,
            {"jon smith": {"action": "attach", "profile_id": "p-foreign"}},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "invalid_attach")

    def test_attach_in_pool_but_not_this_name_candidates_is_invalid(self):
        result = _plan_import_name_resolutions(
            ["Jon Smith"],
            {},
            self.POOL,
            {"jon smith": {"action": "attach", "profile_id": "p-ada"}},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "invalid_attach")

    def test_attach_on_key_with_no_candidates_is_ignored_create(self):
        result = _plan_import_name_resolutions(
            ["Zed Unique"],
            {},
            self.POOL,
            {"zed unique": {"action": "attach", "profile_id": "p-john"}},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["outcomes"]["zed unique"], ("create", None))
        self.assertEqual(result["attaches"], [])

    def test_attach_on_roster_key_is_ignored(self):
        result = _plan_import_name_resolutions(
            ["Jon Smith"],
            {"jon smith": "p-here"},
            self.POOL,
            {"jon smith": {"action": "attach", "profile_id": "p-john"}},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["outcomes"]["jon smith"], ("roster", "p-here"))
        self.assertEqual(result["attaches"], [])

    def test_candidates_without_resolution_are_unresolved(self):
        result = _plan_import_name_resolutions(
            ["Jon Smith"],
            {},
            self.POOL,
            None,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "unresolved")
        self.assertEqual(result["unresolved_names"], ["Jon Smith"])

    def test_create_even_when_pool_has_exact_name_candidate(self):
        result = _plan_import_name_resolutions(
            ["John Smith"],
            {},
            self.POOL,
            {"john smith": {"action": "create"}},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["outcomes"]["john smith"], ("create", None))
        self.assertEqual(result["attaches"], [])

    def test_two_keys_attaching_same_profile_is_duplicate(self):
        result = _plan_import_name_resolutions(
            ["Jon Smith", "Johnny Smith"],
            {},
            [
                {
                    "profile_id": "p-john",
                    "full_name": "John Smith",
                    "sort_name": "Smith, John",
                    "class_id": "class-b",
                    "class_name": "Algebra 2",
                },
            ],
            {
                "jon smith": {"action": "attach", "profile_id": "p-john"},
                "johnny smith": {"action": "attach", "profile_id": "p-john"},
            },
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "duplicate_attach")

    def test_three_spellings_share_one_outcome(self):
        result = _plan_import_name_resolutions(
            ["Jon Smith", "jon  smith", "Smith, Jon"],
            {},
            self.POOL,
            {"jon smith": {"action": "attach", "profile_id": "p-john"}},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["outcomes"]["jon smith"], ("attach", "p-john"))
        self.assertEqual(result["outcomes"]["smith jon"], ("attach", "p-john"))
        self.assertEqual(len(result["attaches"]), 1)
        self.assertEqual(result["attaches"][0][1], "p-john")
        self.assertEqual(result["attaches"][0][2], "Jon Smith")

    def test_malformed_list_instead_of_dict(self):
        result = _plan_import_name_resolutions(["Jon Smith"], {}, self.POOL, [])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "malformed")

    def test_malformed_non_string_profile_id(self):
        result = _plan_import_name_resolutions(
            ["Jon Smith"],
            {},
            self.POOL,
            {"jon smith": {"action": "attach", "profile_id": 12}},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "malformed")

    def test_malformed_unknown_action(self):
        result = _plan_import_name_resolutions(
            ["Jon Smith"],
            {},
            self.POOL,
            {"jon smith": {"action": "merge"}},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "malformed")

    def test_malformed_too_many_entries(self):
        blob = {f"name {i}": {"action": "create"} for i in range(MAX_IMPORT_ROWS + 1)}
        result = _plan_import_name_resolutions(["Jon Smith"], {}, self.POOL, blob)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "malformed")

    def test_malformed_wins_over_unresolved(self):
        result = _plan_import_name_resolutions(
            ["Jon Smith"],
            {},
            self.POOL,
            [],
        )
        self.assertEqual(result["error_type"], "malformed")
        self.assertEqual(result["unresolved_names"], [])

    def test_empty_pool_creates_non_roster_names(self):
        result = _plan_import_name_resolutions(
            ["Jon Smith", "Ada Lovelace"],
            {"ada lovelace": "p-roster-ada"},
            [],
            None,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["outcomes"]["jon smith"], ("create", None))
        self.assertEqual(result["outcomes"]["ada lovelace"], ("roster", "p-roster-ada"))

    def test_three_spellings_create_share_one_creates_entry(self):
        result = _plan_import_name_resolutions(
            ["Jon Smith", "jon  smith", "Smith, Jon"],
            {},
            [],
            None,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["outcomes"]["jon smith"], ("create", None))
        self.assertEqual(result["outcomes"]["smith jon"], ("create", None))
        self.assertEqual(result["creates"], [("jon smith", "Jon Smith")])
        self.assertEqual(result["attaches"], [])


class _ImportWriteRecorder:
    def __init__(self, roster_rows=None, existing_enrollments=None, attach_log_error=None):
        self.roster_rows = roster_rows or []
        self.existing_enrollments = existing_enrollments or []
        self.attach_log_error = attach_log_error
        self.inserts = {}
        self.upserts = {}
        self.full_name_lookups = []

    def table(self, name):
        return _ImportWriteQuery(self, name)

    def write_rows(self, table):
        return list(self.inserts.get(table, [])) + list(self.upserts.get(table, []))

    def zero_writes(self):
        write_tables = (
            "learning_objectives",
            "assignment_objectives",
            "profiles",
            "enrollments",
            "grades",
            "enrollment_attach_log",
            "homework_scores",
        )
        return all(not self.write_rows(t) for t in write_tables)


class _ImportWriteQuery:
    def __init__(self, recorder, name):
        self.recorder = recorder
        self.name = name
        self._eq = {}
        self._in = {}
        self._op = None

    def select(self, *args, **kwargs):
        return self

    def eq(self, field, val):
        self._eq[field] = val
        return self

    def in_(self, field, vals):
        self._in[field] = list(vals)
        if self.name == "profiles" and field == "full_name":
            self.recorder.full_name_lookups.append(list(vals))
        return self

    def insert(self, rows):
        self._op = "insert"
        payload = rows if isinstance(rows, list) else [rows]
        self.recorder.inserts.setdefault(self.name, []).extend(payload)
        if self.name == "enrollment_attach_log" and self.recorder.attach_log_error:
            raise self.recorder.attach_log_error
        return self

    def upsert(self, rows, **kwargs):
        self._op = "upsert"
        self.recorder.upserts.setdefault(self.name, []).extend(rows)
        return self

    def delete(self):
        self._op = "delete"
        return self

    def execute(self):
        if self._op == "insert" and self.name == "learning_objectives":
            return MagicMock(data=[{"id": "lo-new"}])
        if self._op in ("insert", "upsert", "delete"):
            return MagicMock(data=[])
        if self.name == "enrollments":
            if "student_id" in self._in:
                return MagicMock(data=self.recorder.existing_enrollments)
            return MagicMock(data=self.recorder.roster_rows)
        if self.name == "learning_objectives":
            return MagicMock(data=[])
        if self.name == "assignment_objectives":
            return MagicMock(data=[])
        return MagicMock(data=[])


class TestImportGradesNameResolutions(unittest.TestCase):
    POOL = [
        {
            "profile_id": "p-john",
            "full_name": "John Smith",
            "sort_name": "Smith, John",
            "class_id": "class-b",
            "class_name": "Algebra 2",
        },
        {
            "profile_id": "p-ada",
            "full_name": "Ada Lovelace",
            "sort_name": "Lovelace, Ada",
            "class_id": "class-b",
            "class_name": "Algebra 2",
        },
    ]

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def _post_import(self, recorder, students, name_resolutions=None, pool=None, pool_error=None):
        from app import routes as r
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["csrf_token"] = "test-csrf"
        payload = {
            "class_id": "class-a",
            "assignment_id": "asg-1",
            "students": students,
            "learning_objectives": ["A.1"],
        }
        if name_resolutions is not None:
            payload["name_resolutions"] = name_resolutions
        if pool_error is not None:
            pool_cm = unittest.mock.patch.object(
                r, "_list_same_instructor_other_class_enrollments",
                side_effect=pool_error,
            )
        else:
            pool_cm = unittest.mock.patch.object(
                r, "_list_same_instructor_other_class_enrollments",
                return_value=list(pool) if pool is not None else [],
            )
        with unittest.mock.patch.object(r, "_rate_limit", return_value=True), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_assignment_belongs_to_class", return_value=True), \
                unittest.mock.patch.object(r, "supabase_admin", recorder), \
                unittest.mock.patch.object(
                    r, "_profile_ids_with_enrollment_elsewhere", return_value=set()
                ), \
                unittest.mock.patch.object(
                    r.Homework, "resolve_hw_group_storage_key", return_value="HW1"
                ), \
                unittest.mock.patch.object(
                    r.Homework, "get_hw_scores_map_for_assignment", return_value={}
                ), \
                pool_cm:
            return self.client.post(
                "/api/import-grades",
                json=payload,
                headers={"X-CSRF-Token": "test-csrf"},
            )

    def test_tampered_attach_foreign_profile_is_400_zero_writes(self):
        rec = _ImportWriteRecorder()
        rv = self._post_import(
            rec,
            [{"name": "Jon Smith", "grades": {"A.1": "A"}}],
            name_resolutions={"jon smith": {"action": "attach", "profile_id": "p-foreign"}},
            pool=self.POOL,
        )
        self.assertEqual(rv.status_code, 400)
        self.assertFalse(rv.get_json()["success"])
        self.assertTrue(rec.zero_writes())

    def test_attach_same_instructor_id_not_in_name_candidates_is_400_zero_writes(self):
        rec = _ImportWriteRecorder()
        rv = self._post_import(
            rec,
            [{"name": "Jon Smith", "grades": {"A.1": "A"}}],
            name_resolutions={"jon smith": {"action": "attach", "profile_id": "p-ada"}},
            pool=self.POOL,
        )
        self.assertEqual(rv.status_code, 400)
        self.assertFalse(rv.get_json()["success"])
        self.assertTrue(rec.zero_writes())

    def test_missing_resolution_for_flagged_name_is_409_zero_writes(self):
        rec = _ImportWriteRecorder()
        rv = self._post_import(
            rec,
            [{"name": "Jon Smith", "grades": {"A.1": "A"}}],
            pool=self.POOL,
        )
        self.assertEqual(rv.status_code, 409)
        body = rv.get_json()
        self.assertFalse(body["success"])
        self.assertEqual(body["unresolved_names"], ["Jon Smith"])
        self.assertTrue(rec.zero_writes())
        self.assertEqual(rec.write_rows("learning_objectives"), [])

    def test_create_does_not_attach_global_exact_full_name_profile(self):
        rec = _ImportWriteRecorder()
        rv = self._post_import(
            rec,
            [{"name": "John Smith", "grades": {"A.1": "A"}}],
            pool=[],
        )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rec.full_name_lookups, [])
        profiles = rec.write_rows("profiles")
        self.assertEqual(len(profiles), 1)
        new_id = profiles[0]["id"]
        self.assertNotEqual(new_id, "p-old-global")
        enrolled = [row["student_id"] for row in rec.write_rows("enrollments")]
        self.assertEqual(enrolled, [new_id])
        self.assertNotIn("p-old-global", enrolled)

    def test_two_names_attaching_one_profile_is_400_zero_writes(self):
        rec = _ImportWriteRecorder()
        rv = self._post_import(
            rec,
            [
                {"name": "Jon Smith", "grades": {"A.1": "A"}},
                {"name": "Johnny Smith", "grades": {"A.1": "A"}},
            ],
            name_resolutions={
                "jon smith": {"action": "attach", "profile_id": "p-john"},
                "johnny smith": {"action": "attach", "profile_id": "p-john"},
            },
            pool=[self.POOL[0]],
        )
        self.assertEqual(rv.status_code, 400)
        self.assertTrue(rec.zero_writes())

    def test_h3_foreign_name_with_no_resolution_creates_new_profile(self):
        rec = _ImportWriteRecorder()
        rv = self._post_import(
            rec,
            [{"name": "John Smith", "grades": {"A.1": "A"}}],
            pool=[],
        )
        self.assertEqual(rv.status_code, 200)
        profiles = rec.write_rows("profiles")
        self.assertEqual(len(profiles), 1)
        new_id = profiles[0]["id"]
        self.assertNotEqual(new_id, "p-foreign-john")
        enrolled = [row["student_id"] for row in rec.write_rows("enrollments")]
        self.assertEqual(enrolled, [new_id])
        self.assertNotIn("p-foreign-john", enrolled)

    def test_candidate_pool_load_failure_is_500_zero_writes(self):
        rec = _ImportWriteRecorder()
        rv = self._post_import(
            rec,
            [{"name": "Jon Smith", "grades": {"A.1": "A"}}],
            pool_error=RuntimeError("pool lookup failed"),
        )
        self.assertEqual(rv.status_code, 500)
        self.assertTrue(rec.zero_writes())

    def test_this_class_roster_name_uses_enrolled_id_without_resolution(self):
        rec = _ImportWriteRecorder(
            roster_rows=[{
                "student_id": "p-here",
                "profiles": {"id": "p-here", "full_name": "John Smith"},
            }],
            existing_enrollments=[{"student_id": "p-here"}],
        )
        rv = self._post_import(
            rec,
            [{"name": "John Smith", "grades": {"A.1": "A"}}],
            pool=self.POOL,
        )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rec.write_rows("profiles"), [])
        self.assertEqual(rec.write_rows("enrollments"), [])
        grades = rec.write_rows("grades")
        self.assertEqual(len(grades), 1)
        self.assertEqual(grades[0]["student_id"], "p-here")

    def test_valid_attach_enrolls_candidate_and_writes_attach_log(self):
        rec = _ImportWriteRecorder()
        rv = self._post_import(
            rec,
            [{"name": "Jon Smith", "grades": {"A.1": "A"}}],
            name_resolutions={"jon smith": {"action": "attach", "profile_id": "p-john"}},
            pool=self.POOL,
        )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rec.write_rows("profiles"), [])
        enrolled = [row["student_id"] for row in rec.write_rows("enrollments")]
        self.assertEqual(enrolled, ["p-john"])
        logs = rec.write_rows("enrollment_attach_log")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["profile_id"], "p-john")
        self.assertEqual(logs[0]["sheet_name"], "Jon Smith")
        self.assertEqual(logs[0]["class_id"], "class-a")
        self.assertEqual(logs[0]["instructor_id"], "inst1")

    def test_duplicate_spellings_create_share_one_new_profile(self):
        rec = _ImportWriteRecorder()
        rv = self._post_import(
            rec,
            [
                {"name": "Jon Smith", "grades": {"A.1": "A"}},
                {"name": "Smith, Jon", "grades": {"A.1": "A"}},
            ],
            pool=[],
        )
        self.assertEqual(rv.status_code, 200)
        profiles = rec.write_rows("profiles")
        self.assertEqual(len(profiles), 1)
        new_id = profiles[0]["id"]
        grade_ids = [row["student_id"] for row in rec.write_rows("grades")]
        self.assertEqual(grade_ids, [new_id])

    def test_duplicate_grade_conflict_key_last_sheet_row_wins(self):
        from app import routes as r

        rec = _ImportWriteRecorder()
        with unittest.mock.patch.object(
            r.Homework,
            "student_has_recorded_score",
            return_value=True,
        ):
            rv = self._post_import(
                rec,
                [
                    {"name": "John Smith", "grades": {"A.1": "M"}},
                    {"name": "John Smith", "grades": {"A.1": "P"}},
                ],
                pool=[],
            )

        self.assertEqual(rv.status_code, 200)
        grades = rec.write_rows("grades")
        self.assertEqual(len(grades), 1)
        self.assertEqual(grades[0]["top_score"], "P")

        conflict_keys = [
            (
                row["student_id"],
                row["learning_objective_id"],
                row["assignment_id"],
            )
            for row in grades
        ]
        self.assertEqual(len(conflict_keys), len(set(conflict_keys)))

    def test_attach_log_insert_failure_still_writes_grades(self):
        rec = _ImportWriteRecorder(attach_log_error=RuntimeError("table missing"))
        rv = self._post_import(
            rec,
            [{"name": "Jon Smith", "grades": {"A.1": "A"}}],
            name_resolutions={"jon smith": {"action": "attach", "profile_id": "p-john"}},
            pool=self.POOL,
        )
        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.get_json()["success"])
        grades = rec.write_rows("grades")
        self.assertEqual(len(grades), 1)
        self.assertEqual(grades[0]["student_id"], "p-john")


if __name__ == '__main__':
    unittest.main()
