"""Assignment dates accept a real YYYY-MM-DD or empty, and nothing else."""
import unittest
from unittest.mock import MagicMock, patch

from app import create_app

DATE_MESSAGE = "Enter a date as YYYY-MM-DD."
_ARABIC_INDIC = "\u0662\u0660\u0662\u0666-\u0660\u0668-\u0662\u0660"

_ACCEPTED = (
    ("valid", "2026-08-20", "2026-08-20"),
    ("null", None, None),
    ("blank", "", None),
)
_REJECTED = (
    "2026-02-30",
    "2026-13-01",
    "=cmd",
    " 2026-08-20",
    "2026-08-20 ",
    "20260820",
    "2026-W34-4",
    "x" * 400,
    20260820,
    _ARABIC_INDIC,
)


class _Table:
    def __init__(self):
        self.inserts = []
        self.updates = []
        self._writing = ""

    def insert(self, row):
        self.inserts.append(row)
        self._writing = "insert"
        return self

    def update(self, row):
        self.updates.append(row)
        self._writing = "update"
        return self

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def range(self, *args, **kwargs):
        return self

    def delete(self):
        return self

    def in_(self, *args, **kwargs):
        return self

    def execute(self):
        if self._writing == "insert":
            self._writing = ""
            return MagicMock(data=[{"id": "asg-new"}])
        self._writing = ""
        return MagicMock(data=[])


class TestAssignmentCalendarDates(unittest.TestCase):
    def setUp(self):
        app = create_app()
        app.config["TESTING"] = True
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"

    def _post(self, kind, field, value):
        from app import routes as routes_module

        table = _Table()
        admin = MagicMock()
        admin.table.return_value = table
        body = {
            "name": "Quiz 1",
            "homework_group": "hw1",
            "assignment_type": "mastery_opp",
            "selected_los": [],
            "date_returned": None,
            "revision_due": None,
        }
        body[field] = value
        patches = [
            patch.object(routes_module, "supabase_admin", admin),
            patch.object(
                routes_module, "_instructor_owns_class", return_value=True
            ),
            patch.object(
                routes_module.Homework,
                "canonicalize_homework_group_for_class",
                return_value="hw1",
            ),
            patch.object(
                routes_module.Course, "get_lo_ids_for_class", return_value=[]
            ),
        ]
        if kind == "update":
            patches.append(patch.object(
                routes_module, "_assignment_belongs_to_class", return_value=True
            ))
            url = "/class/c1/assignments/asg-1/update"
            method = self.client.put
        else:
            url = "/class/c1/create_assignment"
            method = self.client.post
        for active in patches:
            active.start()
        try:
            response = method(
                url,
                json=body,
                headers={"X-CSRF-Token": "test-csrf"},
            )
        finally:
            for active in reversed(patches):
                active.stop()
        return response, table

    def _assert_rejected(self, kind, field, value):
        response, table = self._post(kind, field, value)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.get_json()), {"success", "error"})
        self.assertEqual(response.get_json(), {
            "success": False,
            "error": DATE_MESSAGE,
        })
        self.assertEqual(table.inserts, [])
        self.assertEqual(table.updates, [])

    def _assert_stored(self, kind, field, value, stored):
        response, table = self._post(kind, field, value)
        self.assertEqual(response.status_code, 200)
        rows = table.inserts if kind == "create" else table.updates
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][field], stored)
        other = "revision_due" if field == "date_returned" else "date_returned"
        self.assertIsNone(rows[0][other])

    def test_create_assignment_dates(self):
        for field in ("date_returned", "revision_due"):
            for label, value, stored in _ACCEPTED:
                with self.subTest(kind="create", field=field, label=label):
                    self._assert_stored("create", field, value, stored)
            for value in _REJECTED:
                with self.subTest(kind="create", field=field, value=value):
                    self._assert_rejected("create", field, value)

    def test_update_assignment_dates(self):
        for field in ("date_returned", "revision_due"):
            for label, value, stored in _ACCEPTED:
                with self.subTest(kind="update", field=field, label=label):
                    self._assert_stored("update", field, value, stored)
            for value in _REJECTED:
                with self.subTest(kind="update", field=field, value=value):
                    self._assert_rejected("update", field, value)
