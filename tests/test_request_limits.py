"""Stage 2 request limits, cache headers, and log redaction."""

import gzip
import logging
import os
import unittest
import unittest.mock

from app import create_app
from app.archive_snapshot import check_snapshot_archive


def _messages(logger_name):
    records = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())
    logger = logging.getLogger(logger_name)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    return logger, handler, records, previous


def _stop(logger, handler, previous):
    logger.removeHandler(handler)
    logger.setLevel(previous)


class TestRequestSize(unittest.TestCase):
    def test_api_body_over_the_limit_is_json_413(self):
        from config import Config
        self.assertEqual(Config.MAX_CONTENT_LENGTH, 20 * 1024 * 1024)
        app = create_app()
        app.config["TESTING"] = True
        app.config["MAX_CONTENT_LENGTH"] = 64
        client = app.test_client()
        response = client.post(
            "/api/login",
            data=b"{" + b"x" * 80,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 413)
        body = response.get_json()
        self.assertEqual(body["success"], False)
        self.assertEqual(body["error"], "Request is too large")

    def test_non_api_body_over_the_limit_is_plain_413(self):
        app = create_app()
        app.config["TESTING"] = True
        app.config["MAX_CONTENT_LENGTH"] = 64
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        response = client.post(
            "/class/c1/archive",
            data=b"confirm_name=" + b"x" * 80,
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(response.status_code, 413)
        self.assertIsNone(response.get_json())
        self.assertEqual(response.data, b"")


class TestGzipCap(unittest.TestCase):
    def test_archive_verification_rejects_a_small_patched_cap(self):
        blob = gzip.compress(b"y" * 300)
        with unittest.mock.patch("app.archive_snapshot.MAX_GZIP_BYTES", 40, create=True):
            try:
                check_snapshot_archive(blob, {})
            except Exception as exc:
                self.assertEqual(type(exc).__name__, "GzipTooLarge")
                return
        self.fail("oversized gzip was accepted")

    def test_restore_rejects_a_small_patched_cap_and_leaves_the_archive(self):
        from app import routes as r
        blob = gzip.compress(b"z" * 400)
        archive_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        row = {
            "id": archive_id,
            "snapshot_path": "inst1/snap.json.gz",
            "storage_path": "inst1/file.zip",
            "restored_at": None,
            "instructor_id": "inst1",
        }
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        with unittest.mock.patch("app.archive_snapshot.MAX_GZIP_BYTES", 40, create=True), \
                unittest.mock.patch.object(r, "_owned_archive_row", return_value=row), \
                unittest.mock.patch("app.archive_storage.download_archive", return_value=blob), \
                unittest.mock.patch.object(r.supabase_admin, "rpc") as rpc, \
                unittest.mock.patch.object(r, "_remove_archive_object") as remove:
            response = client.post(
                f"/archived-classes/{archive_id}/restore",
                data={},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(
            response.get_json()["error"],
            "This archive is too large to restore. Nothing was changed.",
        )
        rpc.assert_not_called()
        remove.assert_not_called()


class TestHeavyRouteRateLimits(unittest.TestCase):
    def setUp(self):
        app = create_app()
        app.config["TESTING"] = True
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"

    def _exhausted(self, method, path):
        from app import routes as r
        with unittest.mock.patch.object(r, "_rate_limit", return_value=False):
            if method == "GET":
                return self.client.get(path)
            return self.client.post(
                path, data={}, headers={"X-CSRF-Token": "test-csrf"}
            )

    def test_archive_create_returns_429(self):
        response = self._exhausted("POST", "/class/c1/archive")
        self.assertEqual(response.status_code, 429)
        self.assertIn("Rate limit", response.get_json()["error"])

    def test_archive_download_returns_429(self):
        response = self._exhausted(
            "GET",
            "/archived-classes/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/download",
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(
            response.get_json()["error"],
            "Rate limit exceeded. Please retry shortly.",
        )

    def test_export_returns_429(self):
        response = self._exhausted("GET", "/class/c1/export-archive")
        self.assertEqual(response.status_code, 429)
        self.assertIn("Rate limit", response.get_json()["error"])

    def test_restore_returns_429(self):
        response = self._exhausted(
            "POST",
            "/archived-classes/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/restore",
        )
        self.assertEqual(response.status_code, 429)
        self.assertIn("Rate limit", response.get_json()["error"])

    def test_revision_send_returns_429(self):
        response = self._exhausted("POST", "/api/class/c1/revision-reminders/send")
        self.assertEqual(response.status_code, 429)
        self.assertIn("Rate limit", response.get_json()["error"])


class TestCacheControl(unittest.TestCase):
    def _session_client(self):
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        return client

    def test_archive_download_and_csv_export_are_no_store(self):
        from app import routes as r
        client = self._session_client()
        archive_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        row = {
            "id": archive_id,
            "class_name": "Algebra",
            "storage_path": "inst1/file.zip",
        }
        logger, handler, records, previous = _messages("app.routes")
        try:
            with unittest.mock.patch.object(r, "_owned_archive_row", return_value=row), \
                    unittest.mock.patch(
                        "app.archive_storage.download_archive", return_value=b"PK"
                    ):
                download = client.get(f"/archived-classes/{archive_id}/download")
        finally:
            _stop(logger, handler, previous)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.headers.get("Cache-Control"), "no-store")
        download_log = "\n".join(records)
        self.assertIn("user_id=inst1", download_log)
        self.assertIn("archive_id=" + archive_id, download_log)
        self.assertNotIn("@", download_log)

        bundle = (
            {"name": "Quiz", "date_returned": "2026-01-02"},
            [{"id": "s1", "full_name": "Lee, Ana"}],
            [],
            {},
        )
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_assignment_belongs_to_class", return_value=True), \
                unittest.mock.patch.object(r, "_gradesheet_export_bundle", return_value=bundle), \
                unittest.mock.patch.object(
                    r.Homework, "get_hw_prev_scores_map_for_assignment", return_value={}
                ):
            exported = client.get("/class/c1/assignments/a1/export-csv")
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(exported.headers.get("Cache-Control"), "no-store")

    def test_static_file_is_not_no_store(self):
        client = self._session_client()
        response = client.get("/static/js/ui_helpers.js")
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.headers.get("Cache-Control"), "no-store")


class TestArchiveZipLimit(unittest.TestCase):
    def test_export_uses_the_shared_byte_limit(self):
        from app import routes as r
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
        manifest = {"class_name": "Algebra"}
        with unittest.mock.patch.object(r, "MAX_ARCHIVE_ZIP_BYTES", 8, create=True), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch(
                    "app.archive_export.build_class_archive_zip",
                    return_value=(b"0123456789", manifest),
                ):
            response = client.get("/class/c1/export-archive")
        self.assertEqual(response.status_code, 413)
        self.assertIn("50 MB", response.get_json()["error"])
        self.assertNotEqual(response.data, b"0123456789")


class TestSensitiveLogs(unittest.TestCase):
    def test_provider_failure_log_has_no_address_or_body(self):
        from app import routes as r
        resp = unittest.mock.MagicMock()
        resp.status_code = 400
        resp.text = "rejected student@school.edu BODYTEXT"
        resp.json.return_value = {"message": "student@school.edu"}
        logger, handler, records, previous = _messages("app.routes")
        try:
            with unittest.mock.patch.dict(
                os.environ,
                {"RESEND_API_KEY": "k", "REPORTS_FROM_EMAIL": "reports@school.edu"},
            ), unittest.mock.patch.object(r.requests, "post", return_value=resp):
                ok, _err = r._send_via_resend(
                    "student@school.edu",
                    "Subject",
                    "Hello",
                    sender_name="Ada",
                )
        finally:
            _stop(logger, handler, previous)
        self.assertFalse(ok)
        text = "\n".join(records)
        self.assertNotIn("@", text)
        self.assertNotIn("BODYTEXT", text)

    def test_create_assignment_does_not_log_the_payload(self):
        from app import routes as r
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        logger, handler, records, previous = _messages("app.routes")
        try:
            with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True):
                client.post(
                    "/class/c1/create_assignment",
                    json={"name": "PAYLOADMARKER", "homework_group": "hw"},
                    headers={"X-CSRF-Token": "test-csrf"},
                )
        finally:
            _stop(logger, handler, previous)
        self.assertFalse(any("PAYLOADMARKER" in message for message in records))

    def test_grade_save_log_names_the_actor_and_class(self):
        from app import routes as r
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["csrf_token"] = "test-csrf"
            sess["role"] = "instructor"
        query = unittest.mock.MagicMock()
        query.upsert.return_value = query
        admin = unittest.mock.MagicMock()
        admin.table.return_value = query
        logger, handler, records, previous = _messages("app.routes")
        try:
            with unittest.mock.patch.object(r, "supabase_admin", admin), \
                    unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                    unittest.mock.patch.object(r, "_assignment_belongs_to_class", return_value=True), \
                    unittest.mock.patch.object(r, "_class_auto_convert_m_enabled", return_value=False), \
                    unittest.mock.patch.object(
                        r, "_enrolled_student_ids_for_class", return_value={"stu-1"}
                    ), \
                    unittest.mock.patch.object(
                        r.Course, "get_assignment_lo_ids", return_value=["lo-1"]
                    ), \
                    unittest.mock.patch.object(
                        r.Homework, "get_hw_scores_map_for_assignment", return_value={"stu-1": 80}
                    ):
                response = client.post(
                    "/api/class/c1/save-grades",
                    json={"assignment_id": "asg-1", "grades": {"stu-1|lo-1": "M"}},
                    headers={"X-CSRF-Token": "test-csrf"},
                )
        finally:
            _stop(logger, handler, previous)
        self.assertEqual(response.status_code, 200)
        text = "\n".join(records)
        self.assertIn("user_id=inst1", text)
        self.assertIn("class_id=c1", text)
        self.assertNotIn("@", text)

    def test_roster_edit_log_names_the_actor_and_class(self):
        from app import routes as r
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        query = unittest.mock.MagicMock()
        admin = unittest.mock.MagicMock()
        admin.table.return_value = query
        logger, handler, records, previous = _messages("app.routes")
        try:
            with unittest.mock.patch.object(r, "supabase_admin", admin), \
                    unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                    unittest.mock.patch.object(r, "_student_enrolled_in_class", return_value=True), \
                    unittest.mock.patch.object(
                        r, "validate_student_name_and_email",
                        return_value=("Lee, Ana", "ada@school.edu", None),
                    ), \
                    unittest.mock.patch.object(
                        r, "_instructor_student_email_index", return_value={}
                    ):
                response = client.post(
                    "/api/class/c1/students/stu-1/update",
                    json={"name": "Lee, Ana", "email": "ada@school.edu"},
                    headers={"X-CSRF-Token": "test-csrf"},
                )
        finally:
            _stop(logger, handler, previous)
        self.assertEqual(response.status_code, 200)
        text = "\n".join(records)
        self.assertIn("user_id=inst1", text)
        self.assertIn("class_id=c1", text)
        self.assertNotIn("@", text)

    def test_report_send_log_names_the_actor_and_class(self):
        from app import routes as r
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
        executed = unittest.mock.MagicMock(
            data=[{
                "student_id": "stu-1",
                "profiles": {"id": "stu-1", "email": "ada@school.edu"},
            }]
        )
        admin = unittest.mock.MagicMock()
        (
            admin.table.return_value
            .select.return_value
            .eq.return_value
            .eq.return_value
            .limit.return_value
            .execute.return_value
        ) = executed
        logger, handler, records, previous = _messages("app.routes")
        try:
            with unittest.mock.patch.object(r, "supabase_admin", admin), \
                    unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                    unittest.mock.patch.object(r, "_report_sender_name", return_value="Ada"), \
                    unittest.mock.patch.object(r, "_email_cap_counts", return_value=(0, {})), \
                    unittest.mock.patch.object(r, "_revision_class_title", return_value="Algebra"), \
                    unittest.mock.patch.object(r, "_send_via_resend", return_value=(True, "")):
                response = client.post(
                    "/api/class/c1/student/stu-1/send-report-email",
                    json={"subject": "Progress", "body": "See attached"},
                    headers={"X-CSRF-Token": "test-csrf"},
                )
        finally:
            _stop(logger, handler, previous)
        self.assertEqual(response.status_code, 200)
        text = "\n".join(records)
        self.assertIn("user_id=inst1", text)
        self.assertIn("class_id=c1", text)
        self.assertNotIn("@", text)

    def test_archive_create_log_names_the_actor_and_class(self):
        from tests.test_app import TestClassArchiveRoute
        case = TestClassArchiveRoute()
        logger, handler, records, previous = _messages("app.routes")
        try:
            response, _log, _bucket, _deleted = case._post()
        finally:
            _stop(logger, handler, previous)
        self.assertEqual(response.status_code, 200)
        text = "\n".join(records)
        self.assertIn("user_id=inst1", text)
        self.assertIn("class_id=c1", text)
        self.assertNotIn("@", text)

    def test_revision_send_log_names_the_actor_and_class(self):
        from app import routes as r
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "test-csrf"
            sess["full_name"] = "Ada Lovelace"
        partition = {
            "recipients": [],
            "due": {},
            "already_reminded": [],
            "no_email": [],
        }
        logger, handler, records, previous = _messages("app.routes")
        try:
            with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                    unittest.mock.patch.object(
                        r, "revision_reminder_partition", return_value=partition
                    ), \
                    unittest.mock.patch.object(r, "_revision_class_title", return_value="Algebra"), \
                    unittest.mock.patch.object(r, "_revision_sender_name", return_value="Ada"), \
                    unittest.mock.patch.object(r, "_attach_revision_messages"), \
                    unittest.mock.patch.object(r, "_send_revision_reminders", return_value=[]), \
                    unittest.mock.patch.dict(
                        os.environ,
                        {"RESEND_API_KEY": "k", "REPORTS_FROM_EMAIL": "reports@school.edu"},
                    ):
                response = client.post(
                    "/api/class/c1/revision-reminders/send",
                    json={},
                    headers={"X-CSRF-Token": "test-csrf"},
                )
        finally:
            _stop(logger, handler, previous)
        self.assertEqual(response.status_code, 200)
        text = "\n".join(records)
        self.assertIn("user_id=inst1", text)
        self.assertIn("class_id=c1", text)
        self.assertNotIn("@", text)
