import os
import unittest
import unittest.mock
from unittest.mock import MagicMock

from app.models import Course, Grade
from app.helpers.report_email import _send_via_resend, instructor_title
from app.helpers.revision_mail import revision_reminder_partition
from app import create_app
from tests.routes.fakes import _revision_row


class TestRevisionReminders(unittest.TestCase):
    def _client(self):
        from app.helpers.rate_limit import _rate_limit_events
        _rate_limit_events.clear()
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "inst-1"
            sess["role"] = "instructor"
            sess["full_name"] = "Lovelace, Ada"
            sess["csrf_token"] = "test-csrf"
        return client

    def _partition(self):
        return {
            "due": "2026-10-24",
            "recipients": [{
                "student_id": "stu-1",
                "name": "Lee, Ana",
                "email": "ana@example.edu",
                "assignments": [{
                    "assignment_id": "a1",
                    "assignment_name": "Quiz",
                    "objectives": [{
                        "lo_code": "E2",
                        "lo_name": "Limits",
                        "show_name": True,
                        "top_score": "R",
                        "learning_objective_id": "lo-1",
                    }, {
                        "lo_code": "E3",
                        "lo_name": "Derivatives",
                        "show_name": True,
                        "top_score": "RQ",
                        "learning_objective_id": "lo-2",
                    }],
                }],
            }, {
                "student_id": "stu-2",
                "name": "Pat Kim",
                "email": "pat@example.edu",
                "assignments": [{
                    "assignment_id": "a1",
                    "assignment_name": "Quiz",
                    "objectives": [{
                        "lo_code": "E2",
                        "lo_name": "Limits",
                        "show_name": True,
                        "top_score": "RQ",
                        "learning_objective_id": "lo-1",
                    }],
                }],
            }],
            "no_email": [{"student_id": "stu-3", "name": "Cho, Cam"}],
            "already_reminded": [{"student_id": "stu-4", "name": "Diaz, Dia"}],
        }

    def _rows(self):
        return [
            _revision_row(
                "stu-1", "Lee, Ana", "2026-10-24", "Quiz", "E2", "Limits",
                assignment_id="a1", learning_objective_id="lo-1",
            ),
            _revision_row(
                "stu-1", "Lee, Ana", "2026-10-24", "Quiz", "E3", "Derivatives",
                assignment_id="a1", top_score="RQ", learning_objective_id="lo-2",
            ),
            _revision_row(
                "stu-1", "Lee, Ana", "2026-10-25", "Later", "E9", "Later objective",
                assignment_id="a9", learning_objective_id="lo-9",
            ),
            _revision_row(
                "stu-3", "Cho, Cam", "2026-10-24", "Quiz", "E2", "Limits",
                assignment_id="a1", learning_objective_id="lo-1",
            ),
            _revision_row(
                "stu-4", "Diaz, Dia", "2026-10-24", "Quiz", "E2", "Limits",
                assignment_id="a1", learning_objective_id="lo-1",
            ),
        ]

    def _students(self):
        return [
            {"id": "stu-1", "full_name": "Lee, Ana", "email": "ana@example.edu"},
            {"id": "stu-3", "full_name": "Cho, Cam", "email": "not-an-email"},
            {"id": "stu-4", "full_name": "Diaz, Dia", "email": "dia@example.edu"},
        ]

    def test_partition_keeps_only_unsent_items_due_in_three_days(self):
        from datetime import date
        from app.helpers.revision_text import _revision_message
        sent = {("stu-1", "a1", "lo-1"), ("stu-4", "a1", "lo-1")}
        with unittest.mock.patch("app.helpers.revision_mail._load_sent_reminder_keys", return_value=sent):
            part = revision_reminder_partition(
                "c1",
                date(2026, 10, 21),
                named_rows=self._rows(),
                students=self._students(),
            )
        self.assertEqual([row["student_id"] for row in part["recipients"]], ["stu-1"])
        objectives = part["recipients"][0]["assignments"][0]["objectives"]
        self.assertEqual([obj["learning_objective_id"] for obj in objectives], ["lo-2"])
        body = _revision_message(
            part["recipients"][0], part["due"], "Algebra", "Professor Lovelace"
        )
        self.assertIn("Assignment: Quiz", body)
        self.assertIn("- E3 Derivatives: Revise with Question (RQ)", body)
        self.assertIn("E3 Derivatives: Revise with Question (RQ)", body)
        self.assertNotIn("E2", body)
        self.assertNotIn("Later", body)
        self.assertEqual([row["student_id"] for row in part["already_reminded"]], ["stu-4"])
        self.assertEqual([row["student_id"] for row in part["invalid"]], ["stu-3"])
        self.assertEqual(part["due"], "2026-10-24")

    def test_signoff_greeting_and_from_header(self):
        from app.helpers.report_email import _reports_from_header
        from app.helpers.revision_text import _revision_message, _revision_signoff
        self.assertEqual(_revision_signoff("Lovelace, Ada"), "Ada Lovelace")
        self.assertEqual(_revision_signoff("Ada Lovelace"), "Ada Lovelace")
        self.assertEqual(instructor_title("Lovelace, Ada"), "Professor Lovelace")
        self.assertEqual(instructor_title("Ada Lovelace"), "Professor Lovelace")
        self.assertEqual(instructor_title("brody estes"), "Professor Estes")
        self.assertEqual(instructor_title("Estes"), "Professor Estes")
        self.assertEqual(instructor_title(""), "Your instructor")
        self.assertEqual(
            _reports_from_header("Ada Lovelace", "reports@claritygrader.net"),
            '"Ada Lovelace via Clarity Grader" <reports@claritygrader.net>',
        )
        quiz = {
            "assignment_name": "Quiz",
            "objectives": [{
                "lo_code": "E2",
                "lo_name": "Limits",
                "show_name": True,
                "top_score": "R",
            }],
        }
        homework = {
            "assignment_name": "Homework",
            "objectives": [{
                "lo_code": "E4",
                "lo_name": "Integrals",
                "show_name": True,
                "top_score": "R",
            }],
        }
        body = _revision_message(
            {"name": "Pat Kim", "assignments": [quiz, homework]},
            "2026-10-24",
            "Algebra | MWF | 1",
            "Professor Lovelace",
        )
        self.assertIn("Hello Pat Kim,", body)
        self.assertIn("You have a revision due in 3 days, on Saturday, Oct 24.", body)
        self.assertIn("Class: Algebra | MWF | 1", body)
        self.assertIn("Assignment: Quiz", body)
        self.assertIn("- E2 Limits: Revise for Mastery", body)
        self.assertIn("Thank you,", body)
        self.assertIn("Professor Lovelace", body)
        self.assertIn(
            "Assignment: Quiz\n- E2 Limits: Revise for Mastery\n\nAssignment: Homework",
            body,
        )
        self.assertIn(
            "Replies to this email are not monitored. Contact your instructor directly.",
            body,
        )

    def test_preview_returns_one_sample_and_escapes_it(self):
        from app.routes import class_email as r
        from app.helpers.report_email import _report_body_html
        partition = self._partition()
        partition["recipients"][0]["name"] = "Lee, <b>x</b>"
        client = self._client()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "revision_reminder_partition", return_value=partition), \
                unittest.mock.patch.object(r, "_revision_class_title", return_value="Algebra"), \
                unittest.mock.patch("app.helpers.revision_mail._send_via_resend") as sender:
            rv = client.post(
                "/api/class/c1/revision-reminders/preview",
                json={},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 200)
        payload = rv.get_json()
        self.assertNotIn("body", payload["recipients"][0])
        self.assertEqual(payload["recipients"][0]["email"], "ana@example.edu")
        self.assertEqual([row["student_id"] for row in payload["no_email"]], ["stu-3"])
        self.assertIn("Class: Algebra", payload["sample_body"])
        self.assertIn("Hello <b>x</b>,", payload["sample_body"])
        self.assertIn("Revise for Mastery", payload["sample_body"])
        self.assertIn("Revise with Question (RQ)", payload["sample_body"])
        self.assertIn("Professor Lovelace", payload["sample_body"])
        self.assertIn("Assignment: Quiz", payload["sample_body"])
        self.assertIn("- E2 Limits: Revise for Mastery", payload["sample_body"])
        self.assertIn("Thank you,", payload["sample_body"])
        self.assertIn("E2", payload["sample_body"])
        self.assertIn("E3", payload["sample_body"])
        html = _report_body_html(payload["sample_body"], payload["subject"])
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)
        self.assertNotIn("<b>x</b>", html)
        sender.assert_not_called()

    def test_send_ignores_a_body_list_and_pauses_between_sends(self):
        from app.routes import class_email as r
        inserted = []
        client = self._client()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "revision_reminder_partition", return_value=self._partition()), \
                unittest.mock.patch.object(r, "_revision_class_title", return_value="Algebra"), \
                unittest.mock.patch("app.helpers.revision_mail._insert_reminder_rows", side_effect=lambda *args: inserted.append(args[1]["student_id"])), \
                unittest.mock.patch("app.helpers.revision_mail._email_cap_counts", return_value=(0, {})), \
                unittest.mock.patch("app.helpers.revision_mail._record_email_send"), \
                unittest.mock.patch("app.helpers.revision_mail.time.sleep") as sleeper, \
                unittest.mock.patch("app.helpers.revision_mail._send_via_resend", return_value=(True, "")) as sender, \
                unittest.mock.patch.dict(os.environ, {"RESEND_API_KEY": "k", "REPORTS_FROM_EMAIL": "a@b.c"}):
            rv = client.post(
                "/api/class/c1/revision-reminders/send",
                json={"recipients": [{"email": "intruder@example.edu"}]},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(sender.call_count, 2)
        self.assertEqual(inserted, ["stu-1", "stu-2"])
        self.assertNotIn("intruder@example.edu", str(sender.call_args_list))
        self.assertEqual(sleeper.call_args_list[0].args[0], 0.5)
        self.assertEqual(rv.get_json()["counts"]["skipped"], 1)
        self.assertEqual(sender.call_args.kwargs["sender_name"], "Ada Lovelace")

    def test_second_send_skips_students_already_reminded(self):
        from app.routes import class_email as r
        partition = self._partition()
        partition["already_reminded"] = partition["already_reminded"] + [
            {"student_id": "stu-1", "name": "Lee, Ana"},
            {"student_id": "stu-2", "name": "Pat Kim"},
        ]
        partition["recipients"] = []
        client = self._client()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "revision_reminder_partition", return_value=partition), \
                unittest.mock.patch.object(r, "_revision_class_title", return_value="Algebra"), \
                unittest.mock.patch("app.helpers.revision_mail._send_via_resend") as sender, \
                unittest.mock.patch.dict(os.environ, {"RESEND_API_KEY": "k", "REPORTS_FROM_EMAIL": "a@b.c"}):
            rv = client.post(
                "/api/class/c1/revision-reminders/send",
                json={},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 200)
        sender.assert_not_called()
        self.assertEqual(rv.get_json()["counts"]["skipped"], 3)

    def test_one_failure_does_not_stop_the_rest_or_record_the_failure(self):
        from app.routes import class_email as r
        inserted = []

        def send(to_email, subject, body_text, *, sender_name, log_provider_body=True):
            return (to_email != "ana@example.edu", "")

        client = self._client()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "revision_reminder_partition", return_value=self._partition()), \
                unittest.mock.patch.object(r, "_revision_class_title", return_value="Algebra"), \
                unittest.mock.patch("app.helpers.revision_mail._insert_reminder_rows", side_effect=lambda *args: inserted.append(args[1]["student_id"])), \
                unittest.mock.patch("app.helpers.revision_mail._email_cap_counts", return_value=(0, {})), \
                unittest.mock.patch("app.helpers.revision_mail._record_email_send"), \
                unittest.mock.patch("app.helpers.revision_mail.time.sleep"), \
                unittest.mock.patch("app.helpers.revision_mail._send_via_resend", side_effect=send), \
                unittest.mock.patch.dict(os.environ, {"RESEND_API_KEY": "k", "REPORTS_FROM_EMAIL": "a@b.c"}):
            rv = client.post(
                "/api/class/c1/revision-reminders/send",
                json={},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        payload = rv.get_json()
        self.assertEqual(payload["counts"]["failed"], 1)
        self.assertEqual(payload["counts"]["sent"], 1)
        self.assertEqual(inserted, ["stu-2"])

    def test_budget_marks_the_rest_not_attempted(self):
        from app.routes import class_email as r
        from app.helpers.revision_mail import _REVISION_EMAIL_BUDGET_SECONDS
        clock = {"now": 0.0}

        def monotonic():
            return clock["now"]

        def sleep(seconds):
            clock["now"] += seconds

        def send(*args, **kwargs):
            clock["now"] += 40
            return True, ""

        partition = self._partition()
        third = dict(partition["recipients"][1])
        third["student_id"] = "stu-9"
        third["email"] = "nine@example.edu"
        third["assignments"] = partition["recipients"][1]["assignments"]
        partition["recipients"] = partition["recipients"] + [third]
        client = self._client()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "revision_reminder_partition", return_value=partition), \
                unittest.mock.patch.object(r, "_revision_class_title", return_value="Algebra"), \
                unittest.mock.patch("app.helpers.revision_mail._insert_reminder_rows"), \
                unittest.mock.patch("app.helpers.revision_mail._email_cap_counts", return_value=(0, {})), \
                unittest.mock.patch("app.helpers.revision_mail._record_email_send"), \
                unittest.mock.patch("app.helpers.revision_mail.time.monotonic", side_effect=monotonic), \
                unittest.mock.patch("app.helpers.revision_mail.time.sleep", side_effect=sleep), \
                unittest.mock.patch("app.helpers.revision_mail._send_via_resend", side_effect=send), \
                unittest.mock.patch.dict(os.environ, {"RESEND_API_KEY": "k", "REPORTS_FROM_EMAIL": "a@b.c"}):
            rv = client.post(
                "/api/class/c1/revision-reminders/send",
                json={},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        payload = rv.get_json()
        self.assertEqual(_REVISION_EMAIL_BUDGET_SECONDS, 60)
        self.assertEqual(payload["counts"]["sent"], 2)
        self.assertEqual(payload["counts"]["not_attempted"], 1)
        self.assertEqual(
            payload["message"],
            "Close this window and choose Email students again to send the rest.",
        )

    def test_insert_failure_is_sent_not_recorded(self):
        from app.routes import class_email as r
        client = self._client()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(r, "revision_reminder_partition", return_value=self._partition()), \
                unittest.mock.patch.object(r, "_revision_class_title", return_value="Algebra"), \
                unittest.mock.patch("app.helpers.revision_mail._insert_reminder_rows", side_effect=RuntimeError("db")), \
                unittest.mock.patch("app.helpers.revision_mail._email_cap_counts", return_value=(0, {})), \
                unittest.mock.patch("app.helpers.revision_mail._record_email_send"), \
                unittest.mock.patch("app.helpers.revision_mail.time.sleep"), \
                unittest.mock.patch("app.helpers.revision_mail._send_via_resend", return_value=(True, "")), \
                unittest.mock.patch.dict(os.environ, {"RESEND_API_KEY": "k", "REPORTS_FROM_EMAIL": "a@b.c"}):
            with self.assertLogs("app.helpers.revision_mail", level="ERROR") as mail_logs, \
                    self.assertNoLogs("app.routes", level="ERROR"):
                rv = client.post(
                    "/api/class/c1/revision-reminders/send",
                    json={},
                    headers={"X-CSRF-Token": "test-csrf"},
                )
        payload = rv.get_json()
        self.assertEqual(payload["counts"]["sent_not_recorded"], 2)
        self.assertIn("may email that student twice", payload["warning"])
        self.assertNotIn("@", "\n".join(mail_logs.output))

    def test_upsert_ignores_duplicate_reminder_rows(self):
        from app.helpers.revision_mail import _insert_reminder_rows
        query = MagicMock()
        admin = MagicMock()
        admin.table.return_value = query
        query.upsert.return_value = query
        student = {
            "student_id": "stu-1",
            "assignments": [{
                "assignment_id": "a1",
                "objectives": [{"learning_objective_id": "lo-1"}],
            }],
        }
        with unittest.mock.patch("app.helpers.revision_mail.supabase_admin", admin):
            _insert_reminder_rows("c1", student, "2026-10-24", "inst-1")
        args, kwargs = query.upsert.call_args
        self.assertEqual(args[0][0]["assignment_id"], "a1")
        self.assertEqual(args[0][0]["learning_objective_id"], "lo-1")
        self.assertTrue(kwargs["ignore_duplicates"])
        self.assertEqual(
            kwargs["on_conflict"],
            "student_id,assignment_id,learning_objective_id,revision_due",
        )
        query.execute.assert_called_once()
        broken = {
            "student_id": "stu-1",
            "assignments": [{"assignment_id": "a1", "objectives": [{}]}],
        }
        with self.assertRaises(KeyError):
            _insert_reminder_rows("c1", broken, "2026-10-24", "inst-1")

    def test_provider_429_is_failed_and_logs_no_address(self):
        resp = MagicMock()
        resp.status_code = 429
        resp.json.return_value = {"message": "slow down ana@example.edu"}
        resp.text = "slow down ana@example.edu"
        with unittest.mock.patch.dict(os.environ, {"RESEND_API_KEY": "k", "REPORTS_FROM_EMAIL": "a@b.c"}), \
                unittest.mock.patch("app.helpers.report_email.requests.post", return_value=resp) as posted, \
                self.assertLogs("app.helpers.report_email", level="ERROR") as logs:
            ok, err = _send_via_resend(
                "ana@example.edu",
                "Subject",
                "Hello",
                sender_name="Ada Lovelace",
            )
        self.assertFalse(ok)
        self.assertEqual(err, "Email provider rejected request.")
        self.assertNotIn("ana@example.edu", "\n".join(logs.output))
        self.assertNotIn("reply_to", posted.call_args.kwargs["json"])

    def test_non_owner_and_missing_csrf_are_forbidden(self):
        from app.routes import class_email as r
        client = self._client()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=False):
            owned = client.post(
                "/api/class/c1/revision-reminders/preview",
                json={},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        missing = client.post("/api/class/c1/revision-reminders/send", json={})
        self.assertEqual(owned.status_code, 403)
        self.assertEqual(missing.status_code, 403)

    def test_missing_email_config_sends_nothing(self):
        from app.routes import class_email as r
        client = self._client()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch("app.helpers.revision_mail._send_via_resend") as sender, \
                unittest.mock.patch.dict(os.environ, {"RESEND_API_KEY": "", "REPORTS_FROM_EMAIL": ""}, clear=False):
            rv = client.post(
                "/api/class/c1/revision-reminders/send",
                json={},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(rv.status_code, 503)
        sender.assert_not_called()

    def _page(self, rows, students, reminder_rows=None, reminder_error=False):
        from app.routes import classes as r
        from datetime import date
        calls = []

        class Query:
            def __init__(self, name):
                self.name = name

            def select(self, *args, **kwargs):
                return self

            def eq(self, *args, **kwargs):
                return self

            def execute(self):
                calls.append(self.name)
                if self.name == "revision_reminders_sent" and reminder_error:
                    raise RuntimeError("reminders down")
                result = MagicMock()
                result.data = reminder_rows or []
                return result

        class_data = {
            "id": "c1",
            "name": "Algebra",
            "days": "MWF",
            "section_number": "1",
            "enrollments": [],
            "learning_objectives": [],
        }
        ready = []
        for student in students:
            item = dict(student)
            item.setdefault("name", item.get("full_name") or "")
            item.setdefault("objective_total", 0)
            item.setdefault("learning_objectives", [])
            ready.append(item)
        client = self._client()
        admin = MagicMock()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch.object(Course, "get_full_class_data", return_value=class_data), \
                unittest.mock.patch.object(Course, "get_learning_objectives", return_value=[]), \
                unittest.mock.patch("app.helpers.class_progress._attached_objective_ids", return_value=set()), \
                unittest.mock.patch.object(r, "load_assignments_for_class", return_value=[]), \
                unittest.mock.patch.object(r, "_process_enrollments", return_value=(ready, ready, {})), \
                unittest.mock.patch.object(Grade, "get_revision_candidates", return_value=rows), \
                unittest.mock.patch.object(r, "local_today", return_value=date(2026, 10, 21)), \
                unittest.mock.patch.object(r, "supabase_admin", admin), \
                unittest.mock.patch("app.helpers.revision_mail.supabase_admin", admin):
            admin.table.side_effect = Query
            rv = client.get("/class/c1")
        return rv, calls

    def test_counter_matches_preview_recipients(self):
        from app.routes import class_email as r
        from datetime import date
        rows = [
            _revision_row(
                "stu-1", "Lee, Ana", "2026-10-24", "Quiz", "E2", "Limits",
                assignment_id="a1", learning_objective_id="lo-1",
            ),
            _revision_row(
                "stu-3", "Cho, Cam", "2026-10-24", "Quiz", "E2", "Limits",
                assignment_id="a1", learning_objective_id="lo-1",
            ),
        ]
        students = [
            {"id": "stu-1", "full_name": "Lee, Ana", "email": "ana@example.edu"},
            {"id": "stu-3", "full_name": "Cho, Cam", "email": ""},
        ]
        page, calls = self._page(rows, students, reminder_rows=[])
        self.assertEqual(page.status_code, 200)
        text = page.get_data(as_text=True)
        self.assertIn("Due in 3 days ★ (1 not emailed)", text)
        self.assertIn("Email students (1)", text)
        self.assertEqual(calls.count("revision_reminders_sent"), 1)
        client = self._client()
        with unittest.mock.patch.object(r, "_instructor_owns_class", return_value=True), \
                unittest.mock.patch("app.helpers.revision_mail.Course.get_full_class_data", return_value={"id": "c1", "name": "Algebra", "enrollments": [], "learning_objectives": []}), \
                unittest.mock.patch("app.helpers.class_progress._attached_objective_ids", return_value=set()), \
                unittest.mock.patch("app.helpers.revision_mail._process_enrollments", return_value=(students, students, {})), \
                unittest.mock.patch("app.helpers.revision_mail.Grade.get_revision_candidates", return_value=rows), \
                unittest.mock.patch.object(r, "local_today", return_value=date(2026, 10, 21)), \
                unittest.mock.patch("app.helpers.revision_mail._load_sent_reminder_keys", return_value=set()), \
                unittest.mock.patch.object(r, "_revision_class_title", return_value="Algebra"):
            preview = client.post(
                "/api/class/c1/revision-reminders/preview",
                json={},
                headers={"X-CSRF-Token": "test-csrf"},
            )
        self.assertEqual(len(preview.get_json()["recipients"]), 1)
        self.assertNotIn("body", preview.get_json()["recipients"][0])

    def test_counter_drops_after_a_recorded_send(self):
        rows = [
            _revision_row(
                "stu-1", "Lee, Ana", "2026-10-24", "Quiz", "E2", "Limits",
                assignment_id="a1", learning_objective_id="lo-1",
            ),
        ]
        students = [{"id": "stu-1", "full_name": "Lee, Ana", "email": "ana@example.edu"}]
        sent = [{
            "student_id": "stu-1",
            "assignment_id": "a1",
            "learning_objective_id": "lo-1",
        }]
        page, _calls = self._page(rows, students, reminder_rows=sent)
        text = page.get_data(as_text=True)
        self.assertIn("All students have been emailed.", text)
        self.assertNotIn("not emailed", text)
        self.assertNotIn("Email students (", text)

    def test_zero_count_has_no_counter_text_for_no_email(self):
        rows = [
            _revision_row(
                "stu-3", "Cho, Cam", "2026-10-24", "Quiz", "E2", "Limits",
                assignment_id="a1", learning_objective_id="lo-1",
            ),
        ]
        students = [{"id": "stu-3", "full_name": "Cho, Cam", "email": "not-an-email"}]
        page, _calls = self._page(rows, students, reminder_rows=[])
        text = page.get_data(as_text=True)
        self.assertIn("No students have a usable email address.", text)
        self.assertNotIn("not emailed", text)

    def test_page_renders_when_the_reminders_query_raises(self):
        rows = [
            _revision_row(
                "stu-1", "Lee, Ana", "2026-10-24", "Quiz", "E2", "Limits",
                assignment_id="a1", learning_objective_id="lo-1",
            ),
        ]
        students = [{"id": "stu-1", "full_name": "Lee, Ana", "email": "ana@example.edu"}]
        page, calls = self._page(rows, students, reminder_error=True)
        text = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("not emailed", text)
        self.assertIn("Email students", text)
        self.assertEqual(calls.count("revision_reminders_sent"), 1)

    def test_template_send_stays_disabled_and_close_reloads(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "templates", "classes", "class_detail.html",
        )
        with open(path, encoding="utf-8") as handle:
            src = handle.read()
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "app", "static", "js", "class_detail.js",
        )
        with open(script_path, encoding="utf-8") as handle:
            page_js = handle.read()
        button_at = src.find('id="revisionEmailBtn"')
        self.assertIn("view.key == '3'", src[button_at - 400:button_at])
        send_fn = page_js.split("function sendRevisionEmails()", 1)[1]
        self.assertIn("emailSendBtn.disabled = true", send_fn)
        self.assertIn("1 student will be emailed.", page_js)
        self.assertIn("Sample message (first student)", page_js)
        self.assertIn("if (missingEmail.length)", page_js)
        self.assertIn("if (reminded.length)", page_js)
        self.assertIn("people.length === 0", page_js)
        self.assertIn("location.reload()", page_js)
        self.assertIn(
            "Close this window and choose Email students again to send the rest.",
            open(os.path.join(os.path.dirname(__file__), "..", "..", "app", "routes", "class_email.py"), encoding="utf-8").read(),
        )
        self.assertNotIn("_REVISION_EMAIL_CAP", open(
            os.path.join(os.path.dirname(__file__), "..", "..", "app", "routes", "class_email.py"),
            encoding="utf-8",
        ).read())
        self.assertNotIn("over_limit", src)
        self.assertNotIn("over_limit", page_js)
