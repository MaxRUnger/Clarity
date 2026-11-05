import logging
import os
from typing import Any, Dict, List

from flask import jsonify, session

from app.helpers.api_input import _require_json_object
from app.helpers.auth_decorators import api_instructor_required
from app.helpers.authentication import supabase_admin
from app.helpers.class_access import _instructor_owns_class
from app.helpers.class_progress import normalize_profile
from app.helpers.local_time import local_today
from app.helpers.rate_limit import rate_limited
from app.helpers.report_email import (
    EMAIL_CAP_UNAVAILABLE,
    EMAIL_HOLD_MESSAGE,
    EMAIL_INVALID_MESSAGE,
    EMAIL_MAX_RECIPIENTS,
    EMAIL_TOO_MANY,
    REPORT_BODY_MAX,
    _ADDRESS_KINDS,
    _cap_block_reason,
    _email_cap_counts,
    _record_email_send,
    _report_footer,
    _report_sender_name,
    _saved_address_problem,
    _send_via_resend,
    email_recipient_hash,
    instructor_title,
)
from app.helpers.revision_mail import (
    _attach_revision_messages,
    _public_reminder_student,
    _reminder_counts,
    _revision_class_title,
    _send_revision_reminders,
    revision_reminder_partition,
)
from app.helpers.revision_text import _revision_sender_name
from app.routes.blueprint import main_bp

logger = logging.getLogger(__name__)

@main_bp.route("/api/class/<class_id>/student/<student_id>/send-report-email", methods=["POST"])
@api_instructor_required
@rate_limited("email_single_report", limit=60, window_sec=3600)
def api_send_single_report_email(class_id, student_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403

    data, err = _require_json_object()
    if err:
        return err[0], err[1]
    subject = str(data.get("subject") or "").strip()
    body = str(data.get("body") or "").strip()
    if not subject or not body:
        return jsonify({"success": False, "error": "Missing subject or body"}), 400
    if len(subject) > 255:
        return jsonify({"success": False, "error": "Subject must be 255 characters or fewer."}), 400
    if len(body) > REPORT_BODY_MAX:
        return jsonify({"success": False, "error": "Body must be 5,000 characters or fewer."}), 400

    try:
        enrollment = (
            supabase_admin.table("enrollments")
            .select("student_id, profiles(id, email, email_set_at)")
            .eq("class_id", class_id)
            .eq("student_id", student_id)
            .limit(1)
            .execute()
        )
        if not enrollment.data:
            return jsonify({"success": False, "error": "Student is not enrolled in this class"}), 404
        profile = normalize_profile({"profiles": enrollment.data[0].get("profiles")})
        to_email, kind = _saved_address_problem(
            profile.get("email"), profile.get("email_set_at")
        )
        class_title = _revision_class_title(class_id)
    except Exception as e:
        logger.error("Error loading enrollment for report email: %s", e)
        return jsonify({"success": False, "error": "Could not load student email"}), 500
    if kind:
        message, status = _ADDRESS_KINDS[kind]
        return jsonify({"success": False, "error": message}), status

    instructor_id = str(session.get("user_id") or "")
    digest = email_recipient_hash(to_email)
    caps = _email_cap_counts(instructor_id, [digest])
    if caps is None:
        return jsonify({"success": False, "error": EMAIL_CAP_UNAVAILABLE}), 503
    reason = _cap_block_reason(caps[0], caps[1].get(digest, 0))
    if reason:
        return jsonify({"success": False, "error": reason}), 429

    sender_name = instructor_title(_report_sender_name())
    ok, err = _send_via_resend(
        to_email,
        subject,
        body + _report_footer(sender_name, class_title),
        sender_name=sender_name,
    )
    if not ok:
        return jsonify({"success": False, "error": err}), 502
    _record_email_send(instructor_id, class_id, student_id, digest, "report")
    logger.info(
        "report send user_id=%s class_id=%s",
        instructor_id,
        class_id,
    )
    return jsonify({"success": True, "to": to_email})

@main_bp.route("/api/class/<class_id>/send-report-emails", methods=["POST"])
@api_instructor_required
@rate_limited("email_bulk_report", limit=20, window_sec=3600)
def api_send_bulk_report_emails(class_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403

    data, err = _require_json_object()
    if err:
        return err[0], err[1]
    reports = data.get("reports")
    if not isinstance(reports, list) or not reports:
        return jsonify({"success": False, "error": "Missing reports payload"}), 400
    if len(reports) > EMAIL_MAX_RECIPIENTS:
        return jsonify({"success": False, "error": EMAIL_TOO_MANY}), 400

    student_ids: List[str] = []
    for item in reports:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("student_id") or "").strip()
        if sid:
            student_ids.append(sid)
    if not student_ids:
        return jsonify({"success": False, "error": "No valid student ids provided"}), 400

    try:
        enrollments = (
            supabase_admin.table("enrollments")
            .select("student_id, profiles(id, full_name, email, email_set_at)")
            .eq("class_id", class_id)
            .in_("student_id", list(set(student_ids)))
            .execute()
        )
    except Exception as e:
        logger.error("Error loading enrollments for bulk report emails: %s", e)
        return jsonify({"success": False, "error": "Could not load student emails"}), 500

    profile_by_student: Dict[str, Dict[str, Any]] = {}
    for row in (enrollments.data or []):
        sid = str(row.get("student_id") or "").strip()
        if sid:
            profile_by_student[sid] = normalize_profile({"profiles": row.get("profiles")})

    sent = 0
    skipped = 0
    failed = 0
    failures: List[str] = []
    sender_name = instructor_title(_report_sender_name())
    class_title = _revision_class_title(class_id)
    ready = []
    for item in reports:
        if not isinstance(item, dict):
            skipped += 1
            continue
        sid = str(item.get("student_id") or "").strip()
        subject = str(item.get("subject") or "").strip()
        body = str(item.get("body") or "").strip()
        student_name = str(item.get("student_name") or sid).strip() or sid
        if not sid or not subject or not body:
            skipped += 1
            continue
        if len(subject) > 255 or len(body) > REPORT_BODY_MAX:
            skipped += 1
            failures.append(f"{student_name}: email content too long")
            continue
        profile = profile_by_student.get(sid) or {}
        to_email, kind = _saved_address_problem(
            profile.get("email"), profile.get("email_set_at")
        )
        if kind == "missing":
            skipped += 1
            failures.append(f"{student_name}: no saved email")
            continue
        if kind:
            skipped += 1
            failures.append(f"{student_name}: {_ADDRESS_KINDS[kind][0]}")
            continue
        ready.append((sid, student_name, subject, body, to_email))

    instructor_id = str(session.get("user_id") or "")
    instructor_count = 0
    per_hash = {}
    if ready:
        caps = _email_cap_counts(
            instructor_id,
            [email_recipient_hash(item[4]) for item in ready],
        )
        if caps is None:
            return jsonify({"success": False, "error": EMAIL_CAP_UNAVAILABLE}), 503
        instructor_count, per_hash = caps

    sent_now = 0
    hash_sent = {}
    for sid, student_name, subject, body, to_email in ready:
        digest = email_recipient_hash(to_email)
        reason = _cap_block_reason(
            instructor_count + sent_now,
            per_hash.get(digest, 0) + hash_sent.get(digest, 0),
        )
        if reason:
            skipped += 1
            failures.append(f"{student_name}: {reason}")
            continue
        ok, err = _send_via_resend(
            to_email,
            subject,
            body + _report_footer(sender_name, class_title),
            sender_name=sender_name,
        )
        if not ok:
            failed += 1
            failures.append(f"{student_name}: {err}")
            continue
        _record_email_send(instructor_id, class_id, sid, digest, "report")
        sent += 1
        sent_now += 1
        hash_sent[digest] = hash_sent.get(digest, 0) + 1

    payload = {
        "sent": sent,
        "skipped": skipped,
        "failed": failed,
        "failures": failures[:20],
    }
    if sent == 0 and failures:
        payload["success"] = False
        payload["error"] = "No emails were sent."
        return jsonify(payload), 502
    payload["success"] = True
    return jsonify(payload)

@main_bp.route("/api/class/<class_id>/revision-reminders/preview", methods=["POST"])
@api_instructor_required
def api_preview_revision_reminders(class_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    today = local_today()
    try:
        partition = revision_reminder_partition(class_id, today)
        signature = instructor_title(session.get("full_name"))
        class_title = _revision_class_title(class_id)
        subject = _attach_revision_messages(partition, class_title, signature)
    except Exception as e:
        logger.error("Revision reminder preview failed for class %s: %s", class_id, e)
        return jsonify({"success": False, "error": "Could not load reminders"}), 500
    recipients = partition["recipients"]
    sample_body = recipients[0]["body"] if recipients else ""
    return jsonify({
        "success": True,
        "subject": subject,
        "sample_body": sample_body,
        "recipients": [_public_reminder_student(student) for student in recipients],
        "no_email": [
            _public_reminder_student(student) for student in partition["no_email"]
        ],
        "invalid": [
            _public_reminder_student(student)
            for student in partition.get("invalid") or []
        ],
        "held": [
            _public_reminder_student(student)
            for student in partition.get("held") or []
        ],
        "already_reminded": [
            _public_reminder_student(student)
            for student in partition["already_reminded"]
        ],
    })

@main_bp.route("/api/class/<class_id>/revision-reminders/send", methods=["POST"])
@api_instructor_required
@rate_limited("revision_send", limit=10, window_sec=3600)
def api_send_revision_reminders(class_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    _data, err = _require_json_object()
    if err:
        return err[0], err[1]
    if not (os.environ.get("RESEND_API_KEY") or "").strip():
        return jsonify({"success": False, "error": "Email is not configured."}), 503
    if not (os.environ.get("REPORTS_FROM_EMAIL") or "").strip():
        return jsonify({"success": False, "error": "Email is not configured."}), 503
    today = local_today()
    try:
        partition = revision_reminder_partition(class_id, today)
        signature = instructor_title(session.get("full_name"))
        sender_name = _revision_sender_name()
        _attach_revision_messages(
            partition, _revision_class_title(class_id), signature
        )
    except Exception as e:
        logger.error("Revision reminder send failed for class %s: %s", class_id, e)
        return jsonify({"success": False, "error": "Could not load reminders"}), 500
    sent_by = str(session.get("user_id") or "")
    results = _send_revision_reminders(
        class_id,
        partition["recipients"],
        partition["due"],
        sender_name,
        sent_by,
    )
    if results is None:
        return jsonify({"success": False, "error": EMAIL_CAP_UNAVAILABLE}), 503
    for student in partition.get("held") or []:
        results.append({
            "student_id": student["student_id"],
            "name": student["name"],
            "status": "held",
            "message": student.get("message") or EMAIL_HOLD_MESSAGE,
        })
    for student in partition.get("invalid") or []:
        results.append({
            "student_id": student["student_id"],
            "name": student["name"],
            "status": "invalid",
            "message": student.get("message") or EMAIL_INVALID_MESSAGE,
        })
    for student in partition["already_reminded"]:
        results.append({
            "student_id": student["student_id"],
            "name": student["name"],
            "status": "skipped",
        })
    counts = _reminder_counts(results)
    message = ""
    warning = ""
    if counts["not_attempted"]:
        message = "Close this window and choose Email students again to send the rest."
    if counts["sent_not_recorded"]:
        warning = "Sending again may email that student twice."
    logger.info(
        "revision send user_id=%s class_id=%s",
        session.get("user_id"),
        class_id,
    )
    return jsonify({
        "success": True,
        "counts": counts,
        "results": results,
        "message": message,
        "warning": warning,
    })
