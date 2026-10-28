"""Relay controls for report and revision email. These assert the intended rules."""

import hashlib
import os
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

from app import create_app
from app.student_names import normalize_email

HOLD = "This address was added or changed less than 24 hours ago."
INVALID = "The saved email address is not valid."
RECIPIENT_CAP = "The daily limit for this recipient has been reached."
INSTRUCTOR_CAP = "The daily limit for this instructor has been reached."
CAP_FAIL = "Email could not be checked against the daily limit. Nothing was sent."
TOO_MANY = "Too many recipients in one request."
FOOTER_SENTENCE = (
    "Replies to this email are not monitored. Contact your instructor directly."
)


def _hash(address):
    return hashlib.sha256(address.encode("utf-8")).hexdigest()


class _Query:
    def __init__(self, table, db):
        self.table = table
        self.db = db
        self.filters = []
        self.payload = None
        self.op = "select"
        self.count_mode = None
        self.page = None

    def select(self, *args, **kwargs):
        self.op = "select"
        self.count_mode = kwargs.get("count")
        return self

    def insert(self, row):
        self.op = "insert"
        self.payload = row
        return self

    def update(self, row):
        self.op = "update"
        self.payload = row
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def in_(self, col, values):
        self.filters.append((col, list(values)))
        return self

    def gte(self, col, val):
        return self

    def lt(self, col, val):
        return self

    def limit(self, count):
        return self

    def order(self, *args, **kwargs):
        self.db.setdefault("orders", []).append((self.table, args))
        return self

    def range(self, start, end):
        self.page = (start, end)
        return self

    def execute(self):
        if self.db.get("boom") == self.table and self.op == "select":
            raise RuntimeError(self.db.get("boom_message") or "cap count failed")
        if self.op == "insert":
            self.db.setdefault("inserts", []).append((self.table, self.payload))
            response = unittest.mock.MagicMock()
            response.data = [self.payload]
            response.count = 0
            return response
        hash_list = None
        for col, val in self.filters:
            if col == "recipient_hash" and isinstance(val, list):
                hash_list = val
        if self.table == "email_sends" and self.op == "select":
            self.db["email_sends_reads"] = self.db.get("email_sends_reads", 0) + 1
            if hash_list is not None:
                self.db["recipient_reads"] = self.db.get("recipient_reads", 0) + 1
        if self.table == "email_sends" and hash_list is not None and self.op == "select":
            rows = []
            for digest in hash_list:
                copies = self.db.get("hash_counts", {}).get(digest, 0)
                rows.extend(
                    {"recipient_hash": digest, "id": "%s-%s" % (digest, index)}
                    for index in range(copies)
                )
        else:
            rows = []
            for row in self.db.get("rows", {}).get(self.table, []):
                keep = True
                for col, val in self.filters:
                    if isinstance(val, list):
                        if row.get(col) not in val:
                            keep = False
                    elif row.get(col) != val:
                        keep = False
                if keep:
                    rows.append(row)
        if self.page is not None:
            start, end = self.page
            rows = rows[start:end + 1]
        count = len(rows)
        for col, val in self.filters:
            if col == "instructor_id" and "instructor_count" in self.db:
                count = self.db["instructor_count"]
            if col == "recipient_hash" and not isinstance(val, list):
                count = self.db.get("hash_counts", {}).get(val, 0)
        response = unittest.mock.MagicMock()
        response.data = rows
        response.count = None if self.db.get("omit_count") and self.count_mode == "exact" else count
        return response


def _admin(db):
    admin = unittest.mock.MagicMock()
    admin.table.side_effect = lambda name: _Query(name, db)
    return admin


class TestEmailRelay(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "inst1"
            sess["role"] = "instructor"
            sess["full_name"] = "Estes, Brody"
            sess["csrf_token"] = "test-csrf"

    def _db(self, email="ada@school.edu", email_set_at=None, students=None):
        if students is None:
            students = [{
                "student_id": "stu-1",
                "class_id": "c1",
                "profiles": {
                    "id": "stu-1",
                    "full_name": "Lee, Ana",
                    "email": email,
                    "email_set_at": email_set_at,
                },
            }]
        return {
            "rows": {
                "enrollments": students,
                "classes": [{
                    "id": "c1",
                    "name": "Algebra",
                    "days": "",
                    "section_number": "",
                }],
                "profiles": [{
                    "id": "inst1",
                    "full_name": "Estes, Brody",
                }],
            },
            "inserts": [],
            "instructor_count": 0,
            "hash_counts": {},
        }

    def _post_single(self, db, body="Hello Ana,", subject="Progress", extra=None):
        from app import routes as r
        payload = {"subject": subject, "body": body}
        if extra:
            payload.update(extra)
        with unittest.mock.patch.object(r, "supabase_admin", _admin(db)), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_send_via_resend", return_value=(True, "")) as sender:
            response = self.client.post(
                "/api/class/c1/student/stu-1/send-report-email",
                json=payload,
                headers={"X-CSRF-Token": "test-csrf"},
            )
        return response, sender

    def test_updated_address_is_not_emailed_immediately(self):
        recent = datetime.now(timezone.utc).isoformat()
        response, sender = self._post_single(self._db(email_set_at=recent))
        self.assertNotEqual(response.status_code, 200)
        self.assertIn(HOLD, response.get_json()["error"])
        sender.assert_not_called()

    def test_newly_added_student_is_not_emailed_immediately(self):
        recent = datetime.now(timezone.utc).isoformat()
        response, sender = self._post_single(self._db(email_set_at=recent))
        self.assertNotEqual(response.status_code, 200)
        self.assertIn(HOLD, response.get_json()["error"])
        sender.assert_not_called()

    def test_same_student_can_be_emailed_after_24_hours(self):
        settled = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        response, sender = self._post_single(self._db(email_set_at=settled))
        self.assertEqual(response.status_code, 200)
        sender.assert_called_once()
        self.assertEqual(sender.call_args.args[0], "ada@school.edu")
        self.assertIn("Professor Estes", sender.call_args.args[2])
        self.assertIn("Algebra", sender.call_args.args[2])
        self.assertIn(FOOTER_SENTENCE, sender.call_args.args[2])

    def test_null_email_set_at_is_not_held(self):
        response, sender = self._post_single(self._db(email_set_at=None))
        self.assertEqual(response.status_code, 200)
        sender.assert_called_once()
        self.assertEqual(sender.call_args.args[0], "ada@school.edu")

    def test_posted_address_does_not_replace_the_enrollment_address(self):
        response, sender = self._post_single(
            self._db(email_set_at=None),
            extra={"to": "intruder@example.com", "email": "intruder@example.com"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sender.call_args.args[0], "ada@school.edu")
        self.assertNotIn("intruder@example.com", str(sender.call_args))

    def test_sixth_send_to_one_recipient_is_rejected(self):
        db = self._db(email_set_at=None)
        db["hash_counts"][_hash("ada@school.edu")] = 5
        response, sender = self._post_single(db)
        self.assertNotEqual(response.status_code, 200)
        self.assertIn(RECIPIENT_CAP, response.get_json()["error"])
        sender.assert_not_called()

    def test_401st_send_for_one_instructor_is_rejected(self):
        db = self._db(email_set_at=None)
        db["instructor_count"] = 400
        response, sender = self._post_single(db)
        self.assertNotEqual(response.status_code, 200)
        self.assertIn(INSTRUCTOR_CAP, response.get_json()["error"])
        sender.assert_not_called()

    def test_bulk_call_of_101_students_sends_nothing(self):
        from app import routes as r
        reports = []
        students = []
        for index in range(101):
            sid = "stu-%s" % index
            reports.append({
                "student_id": sid,
                "student_name": "Student %s" % index,
                "subject": "Progress",
                "body": "Hello",
            })
            students.append({
                "student_id": sid,
                "class_id": "c1",
                "profiles": {
                    "id": sid,
                    "full_name": "Student, %s" % index,
                    "email": "s%s@school.edu" % index,
                    "email_set_at": None,
                },
            })
        db = self._db(students=students)
        with unittest.mock.patch.object(r, "supabase_admin", _admin(db)), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_send_via_resend", return_value=(True, "")) as sender:
            response = self.client.post(
                "/api/class/c1/send-report-emails",
                json={"reports": reports},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn(TOO_MANY, response.get_json()["error"])
        sender.assert_not_called()

    def test_cap_count_failure_sends_nothing(self):
        db = self._db(email_set_at=None)
        db["boom"] = "email_sends"
        response, sender = self._post_single(db)
        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["error"], CAP_FAIL)
        sender.assert_not_called()

    def test_payload_has_no_reply_to_and_from_is_unchanged(self):
        from app import routes as r
        posted = unittest.mock.MagicMock()
        posted.return_value.status_code = 200
        posted.return_value.json.return_value = {"id": "re_1"}
        with unittest.mock.patch.dict(os.environ, {
            "RESEND_API_KEY": "k",
            "REPORTS_FROM_EMAIL": "reports@claritygrader.net",
        }), unittest.mock.patch.object(r.requests, "post", posted):
            ok, err = r._send_via_resend(
                "ada@school.edu",
                "Progress",
                "Hello",
                sender_name="Professor Estes",
            )
        self.assertTrue(ok)
        self.assertEqual(err, "")
        payload = posted.call_args.kwargs["json"]
        self.assertNotIn("reply_to", payload)
        self.assertEqual(
            payload["from"],
            '"Professor Estes via Clarity Grader" <reports@claritygrader.net>',
        )

    def test_body_of_5001_characters_is_rejected(self):
        response, sender = self._post_single(self._db(email_set_at=None), body="b" * 5001)
        self.assertEqual(response.status_code, 400)
        sender.assert_not_called()

    def test_footer_names_the_instructor_and_the_class(self):
        response, sender = self._post_single(self._db(email_set_at=None), body="Hello Ana,")
        self.assertEqual(response.status_code, 200)
        body = sender.call_args.args[2]
        self.assertIn("Hello Ana,", body)
        self.assertIn("Professor Estes", body)
        self.assertIn("Class: Algebra", body)
        self.assertIn("You are receiving this because you are enrolled in Algebra.", body)
        self.assertIn(FOOTER_SENTENCE, body)

    def test_both_report_routes_return_429_when_the_burst_limit_is_exhausted(self):
        from app import routes as r
        with unittest.mock.patch.object(r, "_rate_limit", return_value=False):
            single = self.client.post(
                "/api/class/c1/student/stu-1/send-report-email",
                json={"subject": "Progress", "body": "Hello"},
                headers={"X-CSRF-Token": "test-csrf"},
            )
            bulk = self.client.post(
                "/api/class/c1/send-report-emails",
                json={"reports": []},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(single.status_code, 429)
        self.assertEqual(bulk.status_code, 429)
        self.assertIn("Rate limit", single.get_json()["error"])
        self.assertIn("Rate limit", bulk.get_json()["error"])

    def test_angle_bracket_and_comma_are_rejected(self):
        self.assertIsNone(normalize_email("a<b@example.com"))
        self.assertIsNone(normalize_email("doe,jane@example.com"))
        self.assertEqual(normalize_email("Ada@School.edu"), "ada@school.edu")

    def test_invalid_stored_address_is_not_sent(self):
        response, sender = self._post_single(
            self._db(email="not-an-email", email_set_at=None)
        )
        self.assertNotEqual(response.status_code, 200)
        self.assertIn(INVALID, response.get_json()["error"])
        sender.assert_not_called()

    def test_revision_send_ignores_a_posted_recipient_and_records_the_reminder(self):
        from app import routes as r
        partition = {
            "due": "2026-10-24",
            "recipients": [{
                "student_id": "stu-1",
                "name": "Lee, Ana",
                "email": "ana@example.edu",
                "assignments": [{
                    "assignment_id": "a1",
                    "objectives": [{"learning_objective_id": "lo-1"}],
                }],
            }],
            "already_reminded": [],
            "no_email": [],
            "held": [],
            "invalid": [],
        }
        inserted = []
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "revision_reminder_partition", return_value=partition), \
                unittest.mock.patch.object(r, "_revision_class_title", return_value="Algebra"), \
                unittest.mock.patch.object(r, "_insert_reminder_rows", side_effect=lambda *args: inserted.append(args[1]["student_id"])), \
                unittest.mock.patch.object(r, "_email_cap_counts", return_value=(0, {})), \
                unittest.mock.patch.object(r, "_record_email_send"), \
                unittest.mock.patch.object(r.time, "sleep"), \
                unittest.mock.patch.object(r, "_send_via_resend", return_value=(True, "")) as sender, \
                unittest.mock.patch.dict(os.environ, {"RESEND_API_KEY": "k", "REPORTS_FROM_EMAIL": "reports@school.edu"}):
            response = self.client.post(
                "/api/class/c1/revision-reminders/send",
                json={"recipients": [{"email": "intruder@example.com"}]},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(inserted, ["stu-1"])
        self.assertNotIn("intruder@example.com", str(sender.call_args_list))
        self.assertEqual(sender.call_args.args[0], "ana@example.edu")

    def test_footer_is_one_blank_line_before_the_instructor(self):
        from app import routes as r
        settled = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        response, sender = self._post_single(
            self._db(email_set_at=settled),
            body="Hello Ana,\nD1  |  M",
        )
        self.assertEqual(response.status_code, 200)
        text = sender.call_args.args[2]
        self.assertIn("\n\nProfessor Estes\n", text)
        self.assertNotIn("\n\n\nProfessor Estes", text)
        html = r._report_body_html(text, "Progress")
        paragraphs = html.split("<p ")[1:]
        self.assertEqual(len(paragraphs), 2)
        self.assertIn("D1  |  M", paragraphs[0])
        self.assertIn("font-size:14px", paragraphs[0])
        self.assertIn("color:#222222", paragraphs[0])
        self.assertNotIn("color:#666666", paragraphs[0])
        self.assertIn("Professor Estes<br>", paragraphs[1])
        self.assertIn("font-size:12px", paragraphs[1])
        self.assertIn("color:#666666", paragraphs[1])
        self.assertNotIn("D1  |  M", paragraphs[1])

    def test_footer_with_no_trailing_newline_and_with_internal_blank_lines(self):
        from app import routes as r
        bare = "Hello Ana,\nD1  |  M" + r._report_footer("Professor Estes", "Algebra")
        spaced = "Hello Ana,\n\n\nD1  |  M\nD2  |  R" + r._report_footer(
            "Professor Estes", "Algebra"
        )
        for text, grades in (
            (bare, "D1  |  M"),
            (spaced, "D1  |  M<br>D2  |  R"),
        ):
            self.assertIn("\n\nProfessor Estes\n", text)
            self.assertNotIn("\n\n\nProfessor Estes", text)
            paragraphs = r._report_body_html(text, "Progress").split("<p ")[1:]
            self.assertEqual(len(paragraphs), 2 if text is bare else 3)
            grade = paragraphs[-2]
            footer = paragraphs[-1]
            self.assertIn(grades, grade)
            self.assertIn("color:#222222", grade)
            self.assertNotIn("color:#666666", grade)
            self.assertIn("Professor Estes<br>", footer)
            self.assertIn("color:#666666", footer)
            self.assertNotIn("D1  |  M", footer)

    def test_missing_count_sends_nothing(self):
        settled = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        db = self._db(email_set_at=settled)
        db["omit_count"] = True
        response, sender = self._post_single(db)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error"], CAP_FAIL)
        sender.assert_not_called()

    def test_cap_count_log_includes_the_error_and_no_address(self):
        db = self._db()
        db["boom"] = "email_sends"
        db["boom_message"] = "relay-count-error"
        with self.assertLogs("app.routes", level="ERROR") as logs:
            response, sender = self._post_single(db)
        self.assertEqual(response.status_code, 503)
        sender.assert_not_called()
        text = "\n".join(logs.output)
        self.assertIn("email cap count failed user_id=inst1: relay-count-error", text)
        self.assertNotIn("@", text)

    def _count_tables(self, db):
        calls = {"total": 0}
        admin = _admin(db)
        real_table = admin.table.side_effect

        def table(name):
            calls["total"] += 1
            return real_table(name)

        admin.table.side_effect = table
        return admin, calls

    def test_bulk_of_40_reads_email_sends_twice(self):
        from app import routes as r
        students = []
        reports = []
        for index in range(40):
            sid = "stu-%s" % index
            students.append({
                "student_id": sid,
                "class_id": "c1",
                "profiles": {
                    "id": sid,
                    "full_name": "Lee, Ana",
                    "email": "s%s@school.edu" % index,
                    "email_set_at": None,
                },
            })
            reports.append({
                "student_id": sid,
                "student_name": "Lee, Ana",
                "subject": "Progress",
                "body": "Hello Ana,",
            })
        db = self._db(students=students)
        admin, calls = self._count_tables(db)
        with unittest.mock.patch.object(r, "supabase_admin", admin), \
                unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "_send_via_resend", return_value=(True, "")):
            response = self.client.post(
                "/api/class/c1/send-report-emails",
                json={"reports": reports},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["sent"], 40)
        self.assertEqual(calls["total"], 45)
        self.assertEqual(db["email_sends_reads"], 2)

    def test_one_hundred_twenty_hashes_use_three_recipient_reads(self):
        from app import routes as r
        hashes = ["h%03d" % index for index in range(120)]
        db = self._db()
        db["hash_counts"][hashes[0]] = 4
        db["hash_counts"][hashes[50]] = 1
        db["hash_counts"][hashes[119]] = 5
        admin, _calls = self._count_tables(db)
        with self.app.app_context():
            with unittest.mock.patch.object(r, "supabase_admin", admin):
                instructor_count, per_hash = r._email_cap_counts("inst1", hashes)
        self.assertEqual(instructor_count, 0)
        self.assertEqual(db["recipient_reads"], 3)
        self.assertEqual(per_hash[hashes[0]], 4)
        self.assertEqual(per_hash[hashes[50]], 1)
        self.assertEqual(per_hash[hashes[119]], 5)
        self.assertEqual(per_hash[hashes[1]], 0)
        self.assertEqual(
            [item for item in db["orders"] if item[0] == "email_sends"],
            [("email_sends", ("id",))] * 3,
        )
