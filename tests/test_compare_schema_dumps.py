"""Schema dump comparison."""

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "compare_schema_dumps.py"
_SPEC = importlib.util.spec_from_file_location("compare_schema_dumps", _PATH)
compare_schema_dumps = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(compare_schema_dumps)


def _sample():
    return {
        "tables": [{"rls": True, "table": "grades"}, {"rls": True, "table": "profiles"}],
        "columns": [
            {
                "udt": "uuid",
                "type": "uuid",
                "table": "grades",
                "column": "id",
                "default": "gen_random_uuid()",
                "nullable": "NO",
                "position": 1,
            },
            {
                "udt": "uuid",
                "type": "uuid",
                "table": "grades",
                "column": "student_id",
                "default": None,
                "nullable": "NO",
                "position": 8,
            },
            {
                "udt": "text",
                "type": "text",
                "table": "profiles",
                "column": "role",
                "default": None,
                "nullable": "NO",
                "position": 3,
            },
        ],
        "indexes": [],
        "policies": None,
        "functions": [
            {
                "function": "handle_new_user",
                "definition": "BEGIN\r\n  RETURN new;\r\nEND;\r\n",
                "security_definer": True,
            }
        ],
        "extensions": ["plpgsql"],
        "constraints": [
            {
                "name": "grades_student_id_fkey",
                "type": "f",
                "table": "grades",
                "definition": "FOREIGN KEY (student_id) REFERENCES profiles(id) ON DELETE CASCADE",
            }
        ],
        "table_grants": None,
        "table_triggers": [],
        "function_grants": None,
        "storage_buckets": [],
        "storage_policies": None,
        "auth_user_triggers": [],
        "default_privileges": [],
    }


def _csv_text(payload):
    raw = json.dumps(payload)
    return 'dump\n"%s"\n' % raw.replace('"', '""')


class CompareSchemaDumpsTests(unittest.TestCase):
    def test_identical_dumps_give_no_differences(self):
        left = _sample()
        right = copy.deepcopy(left)
        right["columns"][0]["position"] = 40
        right["columns"][1]["position"] = 41
        right["functions"][0]["definition"] = "BEGIN\n  RETURN new;\nEND;\n"
        self.assertEqual(compare_schema_dumps.differences(left, right), [])

    def test_missing_column_is_reported(self):
        left = _sample()
        right = copy.deepcopy(left)
        right["columns"] = [row for row in right["columns"] if row["column"] != "student_id"]
        found = compare_schema_dumps.differences(left, right)
        self.assertIn("columns grades.student_id missing on right", found)

    def test_changed_foreign_key_rule_is_reported(self):
        left = _sample()
        right = copy.deepcopy(left)
        right["constraints"][0]["definition"] = right["constraints"][0]["definition"].replace(
            "ON DELETE CASCADE", "ON DELETE RESTRICT"
        )
        found = "\n".join(compare_schema_dumps.differences(left, right))
        self.assertIn("grades_student_id_fkey", found)
        self.assertIn("ON DELETE CASCADE", found)
        self.assertIn("ON DELETE RESTRICT", found)

    def test_csv_and_plain_json_give_the_same_result(self):
        payload = _sample()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            json_path = root / "plain.json"
            csv_path = root / "export.csv"
            json_path.write_text(json.dumps(payload), encoding="utf-8")
            csv_path.write_text(_csv_text(payload), encoding="utf-8")
            self.assertEqual(
                compare_schema_dumps.differences(
                    compare_schema_dumps.load_dump(json_path),
                    compare_schema_dumps.load_dump(csv_path),
                ),
                [],
            )
            self.assertEqual(compare_schema_dumps.main(["compare", str(json_path), str(csv_path)]), 0)

    def test_udt_difference_is_reported(self):
        left = _sample()
        right = copy.deepcopy(left)
        right["columns"][1]["udt"] = "text"
        found = "\n".join(compare_schema_dumps.differences(left, right))
        self.assertIn("columns grades.student_id udt left=uuid right=text", found)

    def test_csv_larger_than_200000_characters_loads(self):
        payload = _sample()
        payload["functions"][0]["definition"] = "B" * 200001
        with tempfile.TemporaryDirectory() as folder:
            csv_path = Path(folder) / "export.csv"
            csv_path.write_text(_csv_text(payload), encoding="utf-8")
            loaded = compare_schema_dumps.load_dump(csv_path)
        self.assertEqual(loaded["functions"][0]["definition"], "B" * 200001)

    def test_nonempty_policy_or_grant_section_is_reported(self):
        left = _sample()
        for key in ("policies", "table_grants", "function_grants", "storage_policies"):
            right = copy.deepcopy(left)
            right[key] = [{"name": "open"}]
            found = "\n".join(compare_schema_dumps.differences(left, right))
            self.assertIn("%s is not empty" % key, found)


if __name__ == "__main__":
    unittest.main()
