import io
import unittest
import unittest.mock

from app import create_app
from tests.routes.fakes import _ArchiveBucket, _RemovalQuery


class TestClassArchiveExport(unittest.TestCase):
    S1 = "aaaaaaaa-1111-1111-1111-111111111111"
    S2 = "bbbbbbbb-2222-2222-2222-222222222222"
    S3 = "cccccccc-3333-3333-3333-333333333333"
    S4 = "dddddddd-4444-4444-4444-444444444444"
    ASG1 = "11111111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    ASG2 = "22222222-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    def _clear_attached_cache(self):
        from app.helpers.request_cache import _request_cache
        _request_cache().pop(("_attached_objective_ids", "c-archive"), None)

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
        from app.helpers.archive_export import build_class_archive_zip
        from app.models import Course, Homework
        from flask import session
        from app.routes.classes import class_learning_objectives_summary

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
                        "app.helpers.archive_export._load_assignments",
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
                        "app.routes.assignments._gradesheet_export_bundle"
                    ) as bundle, \
                    unittest.mock.patch(
                        "app.helpers.archive_export._gradesheet_letter_map_for_assignment",
                        side_effect=letters,
                    ), \
                    unittest.mock.patch("app.helpers.class_progress.supabase_admin", _Admin()):
                zip_bytes, manifest = build_class_archive_zip("c-archive")
                self.assertEqual(loaded.call_count, 1)
                bundle.assert_not_called()
                app = create_app()
                with app.test_request_context("/class/c-archive/learning-objectives"):
                    session["user_id"] = "inst"
                    session["role"] = "instructor"
                    with unittest.mock.patch("app.routes.classes._instructor_owns_class", return_value=True):
                        html = class_learning_objectives_summary("c-archive")
        finally:
            self._clear_attached_cache()
        return zip_bytes, manifest, hw, prev, html

    def test_zip_matches_the_filled_builder_and_keeps_every_enrollment(self):
        import csv
        import zipfile
        import fitz
        from app.helpers.gradesheet_csv import (
            _build_gradesheet_csv_text,
            _gradesheet_csv_data_rows,
        )
        from app.helpers.lo_progress import (
            learning_objective_cell_text,
            learning_objective_progress_text,
        )
        from app.helpers.student_names import row_sort_key

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
        from app.helpers.archive_export import _student_pdf
        from app.helpers.class_progress import _process_enrollments

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
        from app.helpers.archive_export import archive_entry_stem
        self.assertEqual(archive_entry_stem("Exam 1", "assignment"), "Exam 1")
        self.assertEqual(archive_entry_stem("Estes, Brody", "student"), "Estes, Brody")
        self.assertEqual(archive_entry_stem("Exam 1: A/B?", "assignment"), "Exam 1_ A_B_")
        self.assertEqual(archive_entry_stem("A\nb", "student"), "A_b")
        self.assertEqual(archive_entry_stem("CON", "student"), "CON_")
        self.assertEqual(archive_entry_stem("..", "assignment"), "assignment")

    def test_duplicate_stems_get_the_id_suffix_ignoring_case(self):
        from app.helpers.archive_export import _suffix_duplicate_stems
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
        from app.helpers.archive_export import ArchiveManifestError, _write_entry
        used = set()
        with zipfile.ZipFile(io.BytesIO(), "w") as archive:
            _write_entry(archive, "students/Lee, Ana_gradesheet.pdf", b"a", used)
            with self.assertRaises(ArchiveManifestError):
                _write_entry(archive, "students/LEE, ANA_gradesheet.pdf", b"b", used)

    def test_manifest_check_catches_a_missing_file_and_a_count_mismatch(self):
        import zipfile
        from app.helpers.archive_export import ArchiveManifestError, check_archive_manifest
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
        from app.helpers.rate_limit import _rate_limit_events
        _rate_limit_events.clear()
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
        with unittest.mock.patch("app.routes.archives._instructor_owns_class", return_value=False), \
                unittest.mock.patch("app.helpers.archive_export.build_class_archive_zip") as build:
            response = client.get("/class/c-archive/export-archive")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/instructor/dashboard", response.headers["Location"])
        self.assertFalse(response.data.startswith(b"PK"))
        build.assert_not_called()

    def test_owner_download_names_the_zip_and_deletes_nothing(self):
        from werkzeug.utils import secure_filename
        client = self._client()
        manifest = {"class_name": "Alg II"}
        with unittest.mock.patch("app.routes.archives._instructor_owns_class", return_value=True), \
                unittest.mock.patch(
                    "app.helpers.archive_export.build_class_archive_zip",
                    return_value=(b"PK\x03\x04archive", manifest),
                ), \
                unittest.mock.patch("app.routes.archives.supabase_admin") as admin:
            admin.table.side_effect = AssertionError("export must not touch the database")
            response = client.get("/class/c-archive/export-archive")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"PK\x03\x04archive")
        filename = f"{secure_filename('Alg II') or 'class'}_archive.zip"
        self.assertIn(f'filename="{filename}"', response.headers["Content-Disposition"])

        empty = dict(manifest)
        empty["class_name"] = ""
        with unittest.mock.patch("app.routes.archives._instructor_owns_class", return_value=True), \
                unittest.mock.patch(
                    "app.helpers.archive_export.build_class_archive_zip",
                    return_value=(b"PK\x03\x04archive", empty),
                ):
            fallback = client.get("/class/c-archive/export-archive")
        self.assertIn('filename="class_archive.zip"', fallback.headers["Content-Disposition"])


class TestArchivedClasses(unittest.TestCase):
    ARCHIVE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    OTHER_ID = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    MISSING_ID = "cccccccc-dddd-eeee-ffff-000000000000"
    NEW_CLASS_ID = "dddddddd-eeee-ffff-0000-111111111111"
    FAILED = "Could not restore the class. Nothing was changed."

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

    def _snapshot(self, grades=1, version=1):
        import gzip
        import json
        document = {
            "format_version": version,
            "archived_at": "2026-10-24T00:00:00+00:00",
            "classes": [{"id": "old-class", "name": "Algebra"}],
            "learning_objectives": [],
            "assignments": [],
            "assignment_objectives": [],
            "enrollments": [],
            "grades": [{"id": "g%s" % index, "top_score": "M"} for index in range(grades)],
            "homework_scores": [],
            "free_passes": [],
            "students": [],
        }
        return gzip.compress(json.dumps(document).encode("utf-8"))

    def _restore_row(self):
        row = self._row(self.ARCHIVE_ID, "inst-a", "Algebra")
        row["snapshot_path"] = f"inst-a/{self.ARCHIVE_ID}.snapshot.json.gz"
        row["restored_at"] = None
        return row

    def _restore(self, rows, snapshot, **kwargs):
        bucket = _ArchiveBucket()
        if snapshot is not None:
            bucket.objects[rows[0]["snapshot_path"]] = snapshot
        return self._request(
            "POST",
            f"/archived-classes/{self.ARCHIVE_ID}/restore",
            rows,
            data={},
            bucket=bucket,
            **kwargs,
        )

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
        fail_insert=None,
        rpc_error=None,
        rpc_data=None,
        monotonic=None,
        fail_delete=None,
    ):
        from app.routes import archives as r
        from app.helpers.rate_limit import _rate_limit_events
        import app.helpers.archive_storage as storage

        _rate_limit_events.clear()
        log = []
        seen = {"columns": "", "orders": [], "rpc": []}
        shared = shared if shared is not None else {"row_deletes": 0}
        bucket = bucket or _ArchiveBucket(download_bytes=b"PK\x03\x04archive")

        def table(name):
            query = _RemovalQuery(log, name, rows, fail_delete, fail_insert=fail_insert)
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

        def rpc(fn_name, params):
            seen["rpc"].append((fn_name, params))
            if rpc_error is not None:
                raise rpc_error
            query = unittest.mock.MagicMock()
            query.execute.return_value = unittest.mock.MagicMock(data=rpc_data)
            return query

        admin = unittest.mock.MagicMock()
        admin.table.side_effect = table
        admin.rpc.side_effect = rpc
        admin.storage.from_.return_value = bucket
        client = create_app().test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = user_id
            sess["role"] = role
            sess["csrf_token"] = "test-csrf"
        headers = {"X-CSRF-Token": "test-csrf"} if send_csrf and method != "GET" else {}
        patches = [
            unittest.mock.patch.object(r, "supabase_admin", admin),
            unittest.mock.patch("app.helpers.class_archive.supabase_admin", admin),
            unittest.mock.patch.object(storage, "supabase_admin", admin),
        ]
        if monotonic is not None:
            patches.append(unittest.mock.patch.object(r.time, "monotonic", side_effect=monotonic))
        for item in patches:
            item.start()
        try:
            if method == "GET":
                response = client.get(path)
            else:
                response = client.post(path, data=data or {}, headers=headers)
        finally:
            for item in patches:
                item.stop()
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

    def _card(self, page, class_name):
        marker = ">%s</h2>" % class_name
        start = page.index(marker)
        end = page.find('id="deleteArchiveModal"', start)
        region = page[start:end if end != -1 else None]
        nxt = region.find("<h2", len(marker))
        if nxt == -1:
            return region
        return region[:nxt]

    def test_archive_rows_show_one_restore_state_and_hide_storage_paths(self):
        restored_class_id = "dddddddd-eeee-ffff-0000-111111111111"
        old_id = "eeeeeeee-ffff-0000-1111-222222222222"
        restorable = self._row(self.ARCHIVE_ID, "inst-a", "Restorable Algebra")
        restorable["snapshot_path"] = "inst-a/secret-snapshot.json.gz"
        restorable["storage_path"] = "inst-a/secret-storage.zip"
        restorable["restored_at"] = None
        restorable["restored_class_id"] = None
        restored = self._row(self.OTHER_ID, "inst-a", "Restored Algebra")
        restored["snapshot_path"] = "inst-a/restored-snapshot.json.gz"
        restored["storage_path"] = "inst-a/restored-storage.zip"
        restored["restored_at"] = "2026-10-20T12:00:00+00:00"
        restored["restored_class_id"] = restored_class_id
        deleted_class = self._row(self.MISSING_ID, "inst-a", "Deleted Algebra")
        deleted_class["snapshot_path"] = "inst-a/deleted-snapshot.json.gz"
        deleted_class["storage_path"] = "inst-a/deleted-storage.zip"
        deleted_class["restored_at"] = "2026-10-21T12:00:00+00:00"
        deleted_class["restored_class_id"] = None
        plain = self._row(old_id, "inst-a", "Old Algebra")
        plain["snapshot_path"] = None
        plain["storage_path"] = "inst-a/plain-storage.zip"
        plain["restored_at"] = None
        plain["restored_class_id"] = None
        response, _log, _bucket, seen = self._request(
            "GET",
            "/archived-classes",
            [restorable, restored, deleted_class, plain],
        )
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        from pathlib import Path
        page_script = Path("app/static/js/archived_classes.js").read_text(encoding="utf-8")
        self.assertIn("snapshot_path", seen["columns"])
        self.assertIn("restored_at", seen["columns"])
        self.assertIn("restored_class_id", seen["columns"])
        self.assertNotIn("storage_path", seen["columns"])
        for secret in (
            "secret-snapshot.json.gz",
            "secret-storage.zip",
            "restored-snapshot.json.gz",
            "restored-storage.zip",
            "deleted-snapshot.json.gz",
            "deleted-storage.zip",
            "plain-storage.zip",
            "snapshot_path",
            "storage_path",
        ):
            self.assertNotIn(secret, page)
        restorable_card = self._card(page, "Restorable Algebra")
        restored_card = self._card(page, "Restored Algebra")
        deleted_card = self._card(page, "Deleted Algebra")
        plain_card = self._card(page, "Old Algebra")
        self.assertIn("restore-archive-btn", restorable_card)
        self.assertNotIn("Cannot be restored", restorable_card)
        self.assertNotIn("Restored on", restorable_card)
        self.assertIn("Restored on", restored_card)
        self.assertIn('datetime="2026-10-20T12:00:00+00:00"', restored_card)
        self.assertIn("Open class", restored_card)
        self.assertIn("/class/%s" % restored_class_id, restored_card)
        self.assertNotIn("restore-archive-btn", restored_card)
        self.assertIn("Restored on", deleted_card)
        self.assertIn('datetime="2026-10-21T12:00:00+00:00"', deleted_card)
        self.assertNotIn("Open class", deleted_card)
        self.assertNotIn("restore-archive-btn", deleted_card)
        self.assertIn("Cannot be restored", plain_card)
        self.assertNotIn("restore-archive-btn", plain_card)
        self.assertNotIn("Open class", plain_card)
        self.assertEqual(page.count('class="delete-archive-btn'), 4)
        self.assertEqual(page.count(">Download<"), 4)
        self.assertIn('restoreArchiveTitle.textContent = "Restore " + className + "?"', page_script)
        self.assertNotIn("restoreArchiveTitle.innerHTML", page_script)
        self.assertIn("if (restoreArchiveBusy || !restoreArchiveModal) return;", page_script)
        self.assertIn("if (restoreArchiveBusy || !restoreArchiveId || !restoreArchiveSubmit) return;", page_script)
        self.assertIn("restoreArchiveSubmit.disabled = true;", page_script)
        self.assertIn(
            'fetch("/archived-classes/" + encodeURIComponent(restoreArchiveId) + "/restore"',
            page_script,
        )
        self.assertIn(
            "We couldn't confirm the result. Reload this page to see whether the class was restored.",
            page_script,
        )
        self.assertNotIn("Could not reach the server. Nothing was changed.", page_script)
        self.assertIn(
            "The archive is removed from Archived Classes once the class is restored.",
            page,
        )
        self.assertNotIn("The archive stays in Archived Classes.", page)
        self.assertNotIn("restoreArchiveClose", page)
        script = page_script.split("const restoreNetworkError", 1)[1]
        network_branch = script.split("catch {", 1)[1].split("catch {", 1)[0]
        parse_branch = script.split("catch {", 1)[1].split("catch {", 1)[1].split("payload.success", 1)[0]
        self.assertIn("restoreReloadOnClose = true;", network_branch)
        self.assertIn("showRestoreArchiveError(restoreNetworkError);", network_branch)
        self.assertIn("restoreReloadOnClose = true;", parse_branch)
        self.assertIn("showRestoreArchiveError(restoreNetworkError);", parse_branch)
        branch = script.split("restoreError === restoreAlreadyRestored", 1)[1]
        branch = branch.split("releaseRestoreArchiveSubmit();", 1)[0]
        self.assertIn("showRestoreArchiveError(restoreError);", branch)
        self.assertIn("restoreReloadOnClose = true;", branch)
        self.assertIn("restoreArchiveCancel.disabled = false;", branch)
        self.assertIn("restoreArchiveSubmit.disabled = true;", branch)
        self.assertIn('restoreArchiveSubmit.textContent = "Restore";', branch)
        self.assertNotIn("restoreArchiveSubmit.disabled = false;", branch)
        self.assertIn("if (!restoreError) restoreReloadOnClose = true;", branch)

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

    def test_delete_removes_the_snapshot_after_the_zip(self):
        row = self._row(self.ARCHIVE_ID, "inst-a", "Algebra")
        row["snapshot_path"] = f"inst-a/{self.ARCHIVE_ID}.snapshot.json.gz"
        response, log, bucket, _seen = self._request(
            "POST",
            f"/archived-classes/{self.ARCHIVE_ID}/delete",
            [row],
            data={"confirm_name": "Algebra"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(bucket.calls[0][1], [f"inst-a/{self.ARCHIVE_ID}.zip"])
        self.assertEqual(
            bucket.calls[1][1],
            [f"inst-a/{self.ARCHIVE_ID}.snapshot.json.gz"],
        )
        self.assertTrue(any(
            entry["op"] == "delete" and entry["table"] == "archived_classes" for entry in log
        ))

    def test_an_old_archive_without_snapshot_path_downloads_only_the_zip(self):
        rows = [self._row(self.ARCHIVE_ID, "inst-a", "Algebra")]
        self.assertNotIn("snapshot_path", rows[0])
        listed, _log, list_bucket, _seen = self._request("GET", "/archived-classes", rows)
        self.assertEqual(listed.status_code, 200)
        self.assertIn("Algebra", listed.get_data(as_text=True))
        self.assertEqual(list_bucket.calls, [])
        downloaded, _log, bucket, _seen = self._request(
            "GET", f"/archived-classes/{self.ARCHIVE_ID}/download", rows
        )
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(bucket.calls, [("download", f"inst-a/{self.ARCHIVE_ID}.zip")])

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
        from app.helpers.class_form import CLASS_DAYS
        app = create_app()
        with app.test_request_context("/instructor/dashboard"):
            session["role"] = "instructor"
            html = render_template(
                "classes/instructor_select_class.html",
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

    def test_another_instructor_cannot_restore(self):
        row = self._restore_row()
        row["instructor_id"] = "inst-b"
        response, log, bucket, seen = self._restore([row], self._snapshot())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "This archive was not found.")
        self.assertEqual(bucket.calls, [])
        self.assertEqual(seen["rpc"], [])
        self.assertFalse(any(entry["op"] == "insert" for entry in log))

    def test_null_snapshot_path_is_not_restorable(self):
        row = self._row(self.ARCHIVE_ID, "inst-a", "Algebra")
        response, log, bucket, seen = self._request(
            "POST",
            f"/archived-classes/{self.ARCHIVE_ID}/restore",
            [row],
            data={},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "This archive is not restorable.")
        self.assertEqual(bucket.calls, [])
        self.assertEqual(seen["rpc"], [])
        self.assertFalse(any(entry["op"] == "insert" for entry in log))

    def test_already_restored_does_not_stage(self):
        row = self._restore_row()
        row["restored_at"] = "2026-10-24T00:00:00+00:00"
        response, log, bucket, seen = self._restore([row], self._snapshot())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "This archive has already been restored.")
        self.assertEqual(bucket.calls, [])
        self.assertEqual(seen["rpc"], [])
        self.assertFalse(any(entry["op"] == "insert" for entry in log))

    def test_wrong_format_version_does_not_stage(self):
        response, log, bucket, seen = self._restore(
            [self._restore_row()], self._snapshot(version=2)
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "This archive is not restorable.")
        self.assertEqual([call[0] for call in bucket.calls], ["download"])
        self.assertEqual(seen["rpc"], [])
        self.assertFalse(any(entry["op"] == "insert" for entry in log))

    def test_happy_path_stages_grades_in_two_requests_and_calls_rpc_once(self):
        import logging
        restore_lines = []
        handler = logging.Handler()
        handler.emit = lambda record: restore_lines.append(record.getMessage())
        route_logger = logging.getLogger("app.routes")
        previous_level = route_logger.level
        route_logger.setLevel(logging.INFO)
        route_logger.addHandler(handler)
        try:
            response, log, bucket, seen = self._restore(
                [self._restore_row()],
                self._snapshot(grades=401),
                rpc_data={"class_id": self.NEW_CLASS_ID, "counts": {}},
            )
        finally:
            route_logger.removeHandler(handler)
            route_logger.setLevel(previous_level)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["success"], True)
        self.assertTrue(response.get_json()["redirect"].endswith(f"/class/{self.NEW_CLASS_ID}"))
        restore_log = "\n".join(restore_lines)
        self.assertIn("user_id=inst-a", restore_log)
        self.assertIn("class_id=%s" % self.NEW_CLASS_ID, restore_log)
        self.assertNotIn("@", restore_log)
        grade_inserts = [
            entry["rows"] for entry in log
            if entry["table"] == "archive_restore_stage"
            and entry["op"] == "insert"
            and entry["rows"][0]["table_name"] == "grades"
        ]
        self.assertEqual(len(grade_inserts), 2)
        self.assertEqual(len(grade_inserts[0]), 400)
        self.assertEqual(len(grade_inserts[1]), 1)
        self.assertEqual(grade_inserts[0][0]["seq"], 1)
        self.assertEqual(grade_inserts[0][-1]["seq"], 400)
        self.assertEqual(grade_inserts[1][0]["seq"], 401)
        self.assertEqual(len(seen["rpc"]), 1)
        self.assertEqual(seen["rpc"][0][0], "restore_archived_class")
        expected = seen["rpc"][0][1]["p_expected"]
        self.assertEqual(expected["classes"], 1)
        self.assertEqual(expected["grades"], 401)
        self.assertEqual(expected["students"], 0)
        self.assertEqual(expected["learning_objectives"], 0)
        self.assertEqual(expected["assignments"], 0)
        self.assertEqual(expected["assignment_objectives"], 0)
        self.assertEqual(expected["enrollments"], 0)
        self.assertEqual(expected["homework_scores"], 0)
        self.assertEqual(expected["free_passes"], 0)
        self.assertEqual(seen["rpc"][0][1]["p_archive_id"], self.ARCHIVE_ID)
        self.assertEqual(seen["rpc"][0][1]["p_instructor_id"], "inst-a")
        removes = [call for call in bucket.calls if call[0] == "remove"]
        self.assertEqual(removes, [
            ("remove", [f"inst-a/{self.ARCHIVE_ID}.snapshot.json.gz"]),
            ("remove", [f"inst-a/{self.ARCHIVE_ID}.zip"]),
        ])
        deletes = [
            entry for entry in log
            if entry["op"] == "delete" and entry["table"] == "archived_classes"
        ]
        self.assertEqual(len(deletes), 1)
        self.assertIn(("eq", "id", self.ARCHIVE_ID), deletes[0]["filters"])
        self.assertIn(("eq", "instructor_id", "inst-a"), deletes[0]["filters"])

    def test_snapshot_remove_failure_keeps_the_zip_and_the_row(self):
        row = self._restore_row()
        bucket = _ArchiveBucket(fail_remove_path=row["snapshot_path"])
        bucket.objects[row["snapshot_path"]] = self._snapshot()
        with self.assertLogs("app.routes", level="ERROR") as captured:
            response, log, bucket, _seen = self._request(
                "POST",
                f"/archived-classes/{self.ARCHIVE_ID}/restore",
                [row],
                data={},
                bucket=bucket,
                rpc_data={"class_id": self.NEW_CLASS_ID},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["redirect"].endswith(f"/class/{self.NEW_CLASS_ID}"))
        removes = [call for call in bucket.calls if call[0] == "remove"]
        self.assertEqual(removes, [("remove", [row["snapshot_path"]])])
        self.assertFalse(any(call[1] == [row["storage_path"]] for call in removes))
        self.assertTrue(any(self.ARCHIVE_ID in line for line in captured.output))
        self.assertFalse(any(
            entry["op"] == "delete" and entry["table"] == "archived_classes" for entry in log
        ))

    def test_zip_remove_failure_keeps_the_row(self):
        row = self._restore_row()
        bucket = _ArchiveBucket(fail_remove_path=row["storage_path"])
        bucket.objects[row["snapshot_path"]] = self._snapshot()
        with self.assertLogs("app.routes", level="ERROR") as captured:
            response, log, bucket, _seen = self._request(
                "POST",
                f"/archived-classes/{self.ARCHIVE_ID}/restore",
                [row],
                data={},
                bucket=bucket,
                rpc_data={"class_id": self.NEW_CLASS_ID},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["redirect"].endswith(f"/class/{self.NEW_CLASS_ID}"))
        removes = [call for call in bucket.calls if call[0] == "remove"]
        self.assertEqual(removes, [
            ("remove", [row["snapshot_path"]]),
            ("remove", [row["storage_path"]]),
        ])
        self.assertTrue(any(self.ARCHIVE_ID in line for line in captured.output))
        self.assertFalse(any(
            entry["op"] == "delete" and entry["table"] == "archived_classes" for entry in log
        ))

    def test_row_delete_failure_logs_and_still_redirects(self):
        with self.assertLogs("app.routes", level="ERROR") as captured:
            response, log, bucket, _seen = self._restore(
                [self._restore_row()],
                self._snapshot(),
                rpc_data={"class_id": self.NEW_CLASS_ID},
                fail_delete="archived_classes",
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["redirect"].endswith(f"/class/{self.NEW_CLASS_ID}"))
        removes = [call for call in bucket.calls if call[0] == "remove"]
        self.assertEqual(removes, [
            ("remove", [f"inst-a/{self.ARCHIVE_ID}.snapshot.json.gz"]),
            ("remove", [f"inst-a/{self.ARCHIVE_ID}.zip"]),
        ])
        self.assertTrue(any(self.ARCHIVE_ID in line for line in captured.output))
        self.assertTrue(any(
            entry["op"] == "delete-failed" and entry["table"] == "archived_classes"
            for entry in log
        ))
        self.assertFalse(any(
            entry["op"] == "delete" and entry["table"] == "archived_classes" for entry in log
        ))

    def test_staging_insert_failure_deletes_the_attempt_and_skips_rpc(self):
        response, log, bucket, seen = self._restore(
            [self._restore_row()],
            self._snapshot(),
            fail_insert="archive_restore_stage",
            rpc_data={"class_id": self.NEW_CLASS_ID},
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["error"], self.FAILED)
        self.assertEqual(seen["rpc"], [])
        self.assertTrue(any(
            entry["op"] == "delete" and entry["table"] == "archive_restore_stage" for entry in log
        ))
        self.assertFalse(any(call[0] == "remove" for call in bucket.calls))
        self.assertFalse(any(
            entry["op"] == "delete" and entry["table"] == "archived_classes" for entry in log
        ))

    def test_rpc_error_deletes_the_attempt(self):
        response, log, bucket, seen = self._restore(
            [self._restore_row()],
            self._snapshot(),
            rpc_error=RuntimeError("database unavailable"),
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["error"], self.FAILED)
        self.assertNotIn("database unavailable", response.get_data(as_text=True))
        self.assertEqual(len(seen["rpc"]), 1)
        self.assertTrue(any(
            entry["op"] == "delete" and entry["table"] == "archive_restore_stage" for entry in log
        ))
        self.assertFalse(any(call[0] == "remove" for call in bucket.calls))
        self.assertFalse(any(
            entry["op"] == "delete" and entry["table"] == "archived_classes" for entry in log
        ))

    def test_known_restore_messages_pass_through(self):
        messages = [
            "This archive was not found.",
            "This archive is not restorable.",
            "This archive has already been restored.",
            "A class with this name, days, and section already exists. Archive or rename it first.",
            "A student in this archive no longer has a profile: Lee, Ana",
            "The saved archive could not be read completely.",
            "The restored class did not match the archive. Nothing was restored.",
        ]
        for message in messages:
            response, log, bucket, _seen = self._restore(
                [self._restore_row()],
                self._snapshot(),
                rpc_error=RuntimeError(message),
            )
            self.assertEqual(response.status_code, 409, message)
            self.assertEqual(response.get_json()["error"], message)
            self.assertTrue(any(
                entry["op"] == "delete" and entry["table"] == "archive_restore_stage"
                for entry in log
            ))
            self.assertFalse(any(call[0] == "remove" for call in bucket.calls))
            self.assertFalse(any(
                entry["op"] == "delete" and entry["table"] == "archived_classes" for entry in log
            ))

    def test_unknown_rpc_message_is_generic(self):
        response, _log, _bucket, _seen = self._restore(
            [self._restore_row()],
            self._snapshot(),
            rpc_error=RuntimeError("duplicate key value violates unique constraint"),
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["error"], self.FAILED)
        self.assertNotIn("duplicate key", response.get_data(as_text=True))

    def test_a_staged_row_over_256_kb_returns_the_generic_error(self):
        import gzip
        import json
        document = json.loads(gzip.decompress(self._snapshot(grades=0)))
        document["grades"] = [{"id": "g0", "blob": "x" * (256 * 1024)}]
        snapshot = gzip.compress(json.dumps(document).encode("utf-8"))
        response, log, bucket, seen = self._restore(
            [self._restore_row()],
            snapshot,
            rpc_data={"class_id": self.NEW_CLASS_ID},
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["error"], self.FAILED)
        self.assertEqual(seen["rpc"], [])
        self.assertTrue(any(
            entry["op"] == "delete" and entry["table"] == "archive_restore_stage" for entry in log
        ))
        self.assertFalse(any(call[0] == "remove" for call in bucket.calls))
        self.assertFalse(any(
            entry["op"] == "delete" and entry["table"] == "archived_classes" for entry in log
        ))

    def test_unreadable_rpc_result_redirects_to_the_dashboard(self):
        response, log, bucket, seen = self._restore(
            [self._restore_row()],
            self._snapshot(),
            rpc_data={"counts": {}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["success"], True)
        self.assertTrue(response.get_json()["redirect"].endswith("/instructor/dashboard"))
        self.assertEqual(len(seen["rpc"]), 1)
        self.assertFalse(any(
            entry["op"] == "delete" and entry["table"] == "archive_restore_stage" for entry in log
        ))
        self.assertFalse(any(call[0] == "remove" for call in bucket.calls))
        self.assertFalse(any(
            entry["op"] == "delete" and entry["table"] == "archived_classes" for entry in log
        ))

    def test_budget_after_the_first_chunk_deletes_the_attempt(self):
        response, log, bucket, seen = self._restore(
            [self._restore_row()],
            self._snapshot(grades=1),
            monotonic=[0, 100, 241],
            rpc_data={"class_id": self.NEW_CLASS_ID},
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.get_json()["error"],
            "The restore took too long. Nothing was changed.",
        )
        self.assertEqual(seen["rpc"], [])
        self.assertTrue(any(
            entry["op"] == "delete" and entry["table"] == "archive_restore_stage" for entry in log
        ))
        self.assertFalse(any(call[0] == "remove" for call in bucket.calls))
        self.assertFalse(any(
            entry["op"] == "delete" and entry["table"] == "archived_classes" for entry in log
        ))
