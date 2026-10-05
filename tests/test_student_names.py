"""Literal checks for the single-string student name helpers."""

import csv
import os
import random
import unittest

from app.student_names import (
    display_name,
    normalize_email,
    name_key,
    raw_name_from_csv_fields,
    require_canvas_name,
    row_sort_key,
)

# Canvas roster order used by the name tests and the route tests.
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

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


class TestStudentNameHelpers(unittest.TestCase):
    def test_name_key_keeps_the_comma(self):
        self.assertEqual(name_key("Smith, Cali"), "smith, cali")
        self.assertEqual(name_key("Smith Cali"), "smith cali")

    def test_name_key_collapses_space_around_the_comma(self):
        self.assertEqual(name_key("Smith,Cali"), "smith, cali")
        self.assertEqual(name_key("Smith , Cali"), "smith, cali")

    def test_require_canvas_name_accepts_last_comma_first(self):
        self.assertEqual(require_canvas_name("Smith, Cali"), "Smith, Cali")

    def test_require_canvas_name_collapses_space_around_the_comma(self):
        self.assertEqual(require_canvas_name("Smith ,  Cali"), "Smith, Cali")

    def test_require_canvas_name_rejects_missing_or_empty_sides(self):
        self.assertIsNone(require_canvas_name("Cali Smith"))
        self.assertIsNone(require_canvas_name(", Cali"))

    def test_raw_name_from_separate_columns(self):
        self.assertEqual(raw_name_from_csv_fields("Cali", "Smith"), "Smith, Cali")

    def test_display_name_is_the_stored_string(self):
        self.assertEqual(
            display_name("Benito Velasco Jr, Emilio"),
            "Benito Velasco Jr, Emilio",
        )
        self.assertEqual(display_name(""), "Unnamed student")

    def test_shuffled_canvas_order_sorts_back(self):
        rows = [{"full_name": name} for name in CANVAS_ORDER]
        random.Random(0).shuffle(rows)
        ordered = [row["full_name"] for row in sorted(rows, key=row_sort_key)]
        self.assertEqual(
            ordered,
            [
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
            ],
        )

    def test_blank_name_sorts_after_adams_zoe(self):
        rows = [{"full_name": ""}, {"full_name": "Adams, Zoe"}]
        ordered = [row["full_name"] for row in sorted(rows, key=row_sort_key)]
        self.assertEqual(ordered, ["Adams, Zoe", ""])

    def test_normalize_email_strips_and_lowercases(self):
        self.assertEqual(normalize_email("  Cali@Example.EDU "), "cali@example.edu")
        self.assertEqual(normalize_email("a@b.c"), "a@b.c")
        long_email = ("A" * 243) + "@Example.COM"
        self.assertEqual(len(long_email), 255)
        self.assertEqual(normalize_email(long_email), ("a" * 243) + "@example.com")

    def test_normalize_email_rejects_blank_invalid_and_too_long(self):
        self.assertIsNone(normalize_email(None))
        self.assertIsNone(normalize_email(""))
        self.assertIsNone(normalize_email("   "))
        self.assertIsNone(normalize_email("not-an-email"))
        self.assertIsNone(normalize_email("a@b"))
        self.assertIsNone(normalize_email("a@b."))
        too_long = ("a" * 244) + "@example.com"
        self.assertEqual(len(too_long), 256)
        self.assertIsNone(normalize_email(too_long))


class TestStudentNameFixtures(unittest.TestCase):
    def test_names_file_is_the_canvas_list(self):
        path = os.path.join(_FIXTURES, "students_last_first.csv")
        with open(path, encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], ["Student Name"])
        self.assertEqual([row[0] for row in rows[1:]], list(CANVAS_ORDER))

    def test_email_file_pairs_each_canvas_name(self):
        path = os.path.join(_FIXTURES, "students_last_first_with_emails.csv")
        with open(path, encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], ["Student Name", "email"])
        names = [row[0] for row in rows[1:]]
        emails = [row[1] for row in rows[1:]]
        self.assertEqual(names, list(CANVAS_ORDER))
        self.assertEqual(emails[0], "student01@example.edu")
        self.assertEqual(emails[4], "student05@example.edu")
        self.assertEqual(names[4], "Benito Velasco Jr, Emilio")
        self.assertEqual(emails[17], "student18@example.edu")
        self.assertEqual(names[17], "Juarez Salgado, Evelyn")
        self.assertEqual(emails[28], "student29@example.edu")
        self.assertEqual(names[28], "Smith- Pauley, Kaiya")
        self.assertEqual(emails[29], "student30@example.edu")
        self.assertEqual(names[29], "Smith, Cali")
        self.assertEqual(emails[34], "student35@example.edu")
        self.assertEqual(names[34], "Von Huben, Zachary")
        self.assertEqual(emails[37], "student38@example.edu")
        self.assertEqual(names[37], "Zetina Ariza, Andrew")


if __name__ == "__main__":
    unittest.main()
