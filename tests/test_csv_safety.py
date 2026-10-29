"""Export-time spreadsheet formula neutralization."""
import csv
import io
import re
import unittest
from unittest.mock import MagicMock

from app import create_app
from app.routes import (
    _build_gradesheet_csv_text,
    _enrolled_import_name_index,
    _gradesheet_csv_data_rows,
    _lookup_enrolled_import_student_id,
    parse_blank_gradesheet_csv_text,
    parse_learning_objectives_csv_text,
    parse_students_csv_text,
)

_PLAIN_NEGATIVE = re.compile(r"-\d+(\.\d+)?")
_HOSTILE = ("=cmd", " =cmd", "\t@cmd", "-1+cmd")
_CHECK = "\u2713"


def _leaks(csv_text):
    parsed = list(csv.reader(io.StringIO(csv_text.lstrip("\ufeff"))))
    found = []
    for row_index, row in enumerate(parsed):
        for col_index, cell in enumerate(row):
            probe = cell.lstrip(" \t\r\n")
            if probe == "":
                continue
            first = probe[0]
            if first in ("=", "+", "@"):
                found.append((row_index, col_index, cell))
            elif first == "-" and _PLAIN_NEGATIVE.fullmatch(probe) is None:
                found.append((row_index, col_index, cell))
    return parsed, found


class TestCsvFormulaSafe(unittest.TestCase):
    def _safe(self, value):
        from app.csv_safety import csv_formula_safe
        return csv_formula_safe(value)

    def test_prefixes_formula_triggers(self):
        self.assertEqual(self._safe("=cmd"), "'=cmd")
        self.assertEqual(self._safe("+1"), "'+1")
        self.assertEqual(self._safe("@sum"), "'@sum")
        self.assertEqual(self._safe("-1+cmd"), "'-1+cmd")
        self.assertEqual(self._safe("-Quiz"), "'-Quiz")

    def test_keeps_plain_numbers_dates_and_ordinary_text(self):
        self.assertEqual(self._safe("-1"), "-1")
        self.assertEqual(self._safe("-1.5"), "-1.5")
        self.assertEqual(self._safe("2026-08-20"), "2026-08-20")
        self.assertEqual(self._safe("0/2"), "0/2")
        self.assertEqual(self._safe("85"), "85")
        self.assertEqual(self._safe("M"), "M")
        self.assertEqual(self._safe(_CHECK), _CHECK)

    def test_skips_leading_space_tab_cr_and_lf_before_the_trigger(self):
        self.assertEqual(self._safe(" =1+1"), "' =1+1")
        self.assertEqual(self._safe("\t=1+1"), "'\t=1+1")
        self.assertEqual(self._safe("\r=1+1"), "'\r=1+1")
        self.assertEqual(self._safe("\n=1+1"), "'\n=1+1")

    def test_whitespace_only_apostrophe_and_none(self):
        self.assertEqual(self._safe("  "), "  ")
        self.assertEqual(self._safe("'=cmd"), "'=cmd")
        self.assertEqual(self._safe(None), "")

    def test_csv_safe_row_guards_each_cell(self):
        from app.csv_safety import csv_safe_row
        self.assertEqual(csv_safe_row(["=cmd", "-1", None]), ["'=cmd", "-1", ""])


class TestFormulaWriters(unittest.TestCase):
    def _students(self):
        return [
            {"id": "s%d" % index, "full_name": payload}
            for index, payload in enumerate(_HOSTILE)
        ]

    def _los(self):
        return [
            {"id": "lo%d" % index, "vendor_code": payload}
            for index, payload in enumerate(_HOSTILE)
        ]

    def _assert_sheet(self, csv_text):
        _parsed, found = _leaks(csv_text)
        self.assertEqual(found, [])

    def test_every_writer_neutralizes_hostile_cells(self):
        from app.archive_export import _assignment_csv, _objectives_csv
        from app.models import Course, Homework

        students = self._students()
        los = self._los()
        codes = [lo["vendor_code"] for lo in los]
        hw = {"s0": -1}
        prev = {"s0": -1}
        blank_rows = _gradesheet_csv_data_rows(students, los, hw, {})
        filled_rows = _gradesheet_csv_data_rows(
            students, los, hw, {}, True, prev
        )
        for payload in _HOSTILE:
            with self.subTest(writer="blank", payload=payload):
                self._assert_sheet(_build_gradesheet_csv_text(
                    payload, payload, codes, blank_rows, True, False
                ))
            with self.subTest(writer="filled", payload=payload):
                text = _build_gradesheet_csv_text(
                    payload, payload, codes, filled_rows, False, True
                )
                self._assert_sheet(text)
                self._assert_free_pass(text)
            with self.subTest(writer="archive assignment", payload=payload):
                assignment = {
                    "id": "asg-1",
                    "name": payload,
                    "date_returned": payload,
                }
                with unittest.mock.patch.object(
                    Course, "get_assignment_lo_ids",
                    return_value=[lo["id"] for lo in los],
                ), unittest.mock.patch.object(
                    Homework, "get_hw_scores_map_for_assignment",
                    return_value=hw,
                ), unittest.mock.patch.object(
                    Homework, "get_hw_prev_scores_map_for_assignment",
                    return_value=prev,
                ), unittest.mock.patch(
                    "app.archive_export._gradesheet_letter_map_for_assignment",
                    return_value={},
                ):
                    text = _assignment_csv("c1", assignment, los, students)
                self._assert_sheet(text)
                self._assert_free_pass(text)

        columns = [{"vendor_code": payload} for payload in _HOSTILE]
        grid_rows = [
            {
                "name": payload,
                "cells": [
                    {"display": "0/2"},
                    {"display": "85"},
                    {"display": "M"},
                    {"display": _CHECK},
                ],
                "progress_text": "1/2",
            }
            for payload in _HOSTILE
        ]
        with self.subTest(writer="objectives"):
            self._assert_sheet(_objectives_csv(columns, grid_rows))

    def _assert_free_pass(self, csv_text):
        parsed, _found = _leaks(csv_text)
        cells = [cell for row in parsed for cell in row]
        self.assertIn("-1", cells)
        self.assertNotIn("'-1", cells)

    def test_free_pass_score_is_written_as_minus_one(self):
        students = [{"id": "stu-1", "full_name": "Doe, Jane"}]
        los = [{"id": "lo-1", "vendor_code": "D1"}]
        rows = _gradesheet_csv_data_rows(
            students, los, {"stu-1": -1}, {}, True, {"stu-1": -1}
        )
        text = _build_gradesheet_csv_text(
            "Quiz 1", "2026-08-20", ["D1"], rows, False, True
        )
        parsed = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
        header = next(row for row in parsed if row and row[0] == "Student Name")
        data = next(row for row in parsed if row and "Doe, Jane" in row[0])
        self.assertEqual(data[header.index("HW prev")], "-1")
        self.assertEqual(data[header.index("HW")], "-1")


class TestFormulaImportRoundTrip(unittest.TestCase):
    def test_objective_import_keeps_the_typed_title_and_description(self):
        rows, warnings = parse_learning_objectives_csv_text(
            "title,description,calculation_int\n=D1,=notes,2\n"
        )
        self.assertEqual(warnings, [])
        self.assertEqual(rows[0]["vendor_code"], "=D1")
        self.assertEqual(rows[0]["description"], "=notes")

    def test_formula_name_is_stored_exported_and_matched(self):
        stored = "=Last, First"
        with self.subTest("csv import"):
            rows, warnings = parse_students_csv_text(
                'name,email\n"=Last, First",ada@example.edu\n'
            )
            self.assertEqual(warnings, [])
            self.assertEqual(rows[0]["full_name"], stored)
            self.assertEqual(rows[0]["email"], "ada@example.edu")

        with self.subTest("ui"):
            from app import routes as routes_module
            app = create_app()
            app.config["TESTING"] = True
            client = app.test_client()
            with client.session_transaction() as sess:
                sess["user_id"] = "inst1"
                sess["role"] = "instructor"
                sess["csrf_token"] = "test-csrf"
            query = MagicMock()
            query.insert.return_value = query
            query.select.return_value = query
            query.eq.return_value = query
            query.order.return_value = query
            query.range.return_value = query
            query.in_.return_value = query
            query.execute.return_value = MagicMock(data=[])
            admin = MagicMock()
            admin.table.return_value = query
            with unittest.mock.patch.object(routes_module, "supabase_admin", admin), \
                    unittest.mock.patch.object(
                        routes_module, "_instructor_owns_class", return_value=True
                    ):
                response = client.post(
                    "/class/c1/add_student",
                    json={"name": stored, "email": "ada@example.edu"},
                    headers={"X-CSRF-Token": "test-csrf"},
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["full_name"], stored)
            profile_row = query.insert.call_args_list[0][0][0]
            self.assertEqual(profile_row["full_name"], stored)

        with self.subTest("export and reimport"):
            students = [{"id": "stu-1", "full_name": stored}]
            los = [{"id": "lo-1", "vendor_code": "D1"}]
            data_rows = _gradesheet_csv_data_rows(students, los, {}, {})
            text = _build_gradesheet_csv_text(
                "Quiz 1", "2026-08-20", ["D1"], data_rows, False
            )
            parsed = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
            exported_name = next(
                row[0] for row in parsed if row and row[0].endswith("Last, First")
            )
            self.assertEqual(exported_name, "'=Last, First")
            payload, err = parse_blank_gradesheet_csv_text(text, ["D1"])
            self.assertIsNone(err)
            index = _enrolled_import_name_index([
                {"id": "stu-1", "full_name": stored},
            ])
            self.assertEqual(
                _lookup_enrolled_import_student_id(
                    payload["students"][0]["name"], index
                ),
                "stu-1",
            )
