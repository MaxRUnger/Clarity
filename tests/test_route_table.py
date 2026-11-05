"""Pin every URL rule, its declared methods, and its endpoint."""
import unittest

from app import create_app

# Flask adds HEAD beside GET and OPTIONS on every rule.
EXPECTED = (
    ("/", ("GET",), "main.login_page"),
    ("/add_class", ("POST",), "main.add_class"),
    ("/api/class/<class_id>/assignment/<assignment_id>/grades", ("GET",), "main.api_assignment_grades"),
    ("/api/class/<class_id>/assignments/<assignment_id>/detach-grade-count", ("GET",), "main.api_detach_grade_count"),
    ("/api/class/<class_id>/import-learning-objectives", ("POST",), "main.api_import_learning_objectives"),
    ("/api/class/<class_id>/parse-grade-csv", ("POST",), "main.api_parse_grade_csv"),
    ("/api/class/<class_id>/preview-learning-objectives", ("POST",), "main.api_preview_learning_objectives"),
    ("/api/class/<class_id>/preview-upload-students", ("POST",), "main.api_preview_upload_students"),
    ("/api/class/<class_id>/return_pass", ("POST",), "main.return_free_pass"),
    ("/api/class/<class_id>/revision-reminders/preview", ("POST",), "main.api_preview_revision_reminders"),
    ("/api/class/<class_id>/revision-reminders/send", ("POST",), "main.api_send_revision_reminders"),
    ("/api/class/<class_id>/save-grades", ("POST",), "main.save_grades"),
    ("/api/class/<class_id>/save-hw-percentage", ("POST",), "main.save_hw_percentage"),
    ("/api/class/<class_id>/send-report-emails", ("POST",), "main.api_send_bulk_report_emails"),
    ("/api/class/<class_id>/student/<student_id>/send-report-email", ("POST",), "main.api_send_single_report_email"),
    ("/api/class/<class_id>/students/<student_id>/removal-counts", ("GET",), "main.api_student_removal_counts"),
    ("/api/class/<class_id>/students/<student_id>/update", ("POST",), "main.api_update_student"),
    ("/api/class/<class_id>/toggle_mute", ("POST",), "main.api_toggle_mute"),
    ("/api/class/<class_id>/update-lo/<lo_id>", ("POST",), "main.api_update_learning_objective"),
    ("/api/class/<class_id>/upload_students", ("POST",), "main.api_upload_students_to_class"),
    ("/api/class/<class_id>/use_pass", ("POST",), "main.use_free_pass"),
    ("/api/forgot-password", ("POST",), "main.forgot_password"),
    ("/api/import-grades", ("POST",), "main.api_import_grades"),
    ("/api/login", ("POST",), "main.login"),
    ("/api/reset-password", ("POST",), "main.reset_password"),
    ("/api/session-keepalive", ("POST",), "main.session_keepalive"),
    ("/api/session-status", ("GET",), "main.session_status"),
    ("/api/set-instructor-mode", ("POST",), "main.set_instructor_mode"),
    ("/api/signup", ("POST",), "main.signup"),
    ("/archived-classes", ("GET",), "main.archived_classes"),
    ("/archived-classes/<archive_id>/delete", ("POST",), "main.delete_archived_class"),
    ("/archived-classes/<archive_id>/download", ("GET",), "main.download_archived_class"),
    ("/archived-classes/<archive_id>/restore", ("POST",), "main.restore_archived_class"),
    ("/class/<class_id>", ("GET",), "main.class_detail"),
    ("/class/<class_id>/add_student", ("POST",), "main.add_student"),
    ("/class/<class_id>/archive", ("POST",), "main.archive_class"),
    ("/class/<class_id>/assignments", ("GET",), "main.class_assignments"),
    ("/class/<class_id>/assignments/<assignment_id>/export-blank-csv", ("GET",), "main.export_blank_assignment_csv"),
    ("/class/<class_id>/assignments/<assignment_id>/export-csv", ("GET",), "main.export_assignment_csv"),
    ("/class/<class_id>/assignments/<assignment_id>/update", ("POST", "PUT"), "main.update_assignment"),
    ("/class/<class_id>/copy", ("POST",), "main.copy_class"),
    ("/class/<class_id>/create_assignment", ("POST",), "main.create_assignment"),
    ("/class/<class_id>/create_learning_objective", ("GET", "POST"), "main.create_learning_objective"),
    ("/class/<class_id>/delete", ("POST",), "main.delete_class"),
    ("/class/<class_id>/delete_assignment/<assignment_id>", ("POST",), "main.delete_assignment"),
    ("/class/<class_id>/delete_lo/<lo_id>", ("POST",), "main.delete_lo"),
    ("/class/<class_id>/export-archive", ("GET",), "main.export_class_archive"),
    ("/class/<class_id>/learning-objectives", ("GET",), "main.class_learning_objectives_summary"),
    ("/class/<class_id>/reports", ("GET",), "main.class_reports"),
    ("/class/<class_id>/speed_grader", ("GET",), "main.class_speed_grader"),
    ("/class/<class_id>/student-history/print", ("GET",), "main.student_history_print"),
    ("/class/<class_id>/student/<student_id>/history", ("GET",), "main.student_history"),
    ("/class/<class_id>/students", ("GET",), "main.class_students"),
    ("/class/<class_id>/students/<student_id>", ("GET",), "main.class_student_detail"),
    ("/class/<class_id>/students/<student_id>/delete", ("POST",), "main.delete_student_from_class"),
    ("/class/<class_id>/update", ("POST",), "main.update_class"),
    ("/class/<class_id>/update_grade", ("GET",), "main.upload_grades"),
    ("/forgot-password", ("GET",), "main.forgot_password_page"),
    ("/instructor/dashboard", ("GET",), "main.instructor_dashboard"),
    ("/login", ("GET",), "main.login_page"),
    ("/logout", ("POST",), "main.logout"),
    ("/reset-password", ("GET",), "main.reset_password_page"),
    ("/signup", ("GET",), "main.signup_page"),
)


class TestRouteTable(unittest.TestCase):
    def test_every_url_rule_stays_pinned(self):
        app = create_app()
        found = []
        for rule in app.url_map.iter_rules():
            if rule.endpoint == "static":
                continue
            assert rule.methods is not None
            found.append((
                rule.rule,
                tuple(sorted(rule.methods - {"HEAD", "OPTIONS"})),
                rule.endpoint,
            ))
        self.assertEqual(sorted(found), sorted(EXPECTED))
        self.assertEqual(len(found), 63)
