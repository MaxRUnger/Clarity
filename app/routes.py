"""HTTP routes and API handlers for Clarity.

Deployment (production)
-----------------------
In-memory structures — request rate limits in app.helpers.rate_limit
(``_rate_limit_events``) and one-time form tokens in ``app.helpers.form_tokens``
(``_used_form_tokens``) —
are **not** shared across Gunicorn/Uvicorn workers. With multiple workers,
rate limits and token replay checks can appear broken or inconsistent.

**Options:** run **one worker**; use a load balancer with **sticky sessions** to
the same worker; or replace these stores with **Redis** (or equivalent) before
scaling horizontally.

If you intentionally run multiple workers in production, set environment
variable ``MULTI_WORKER=1`` to log a one-time warning at startup (see
``create_app`` in ``app/__init__.py``).
"""

import hashlib
import logging
import os
import secrets
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Tuple
from urllib.parse import quote
from flask import (  # type: ignore
    Blueprint,
    render_template,
    request,
    jsonify,
    redirect,
    url_for,
    session,
    Response,
)
from werkzeug.utils import secure_filename
from app.helpers.authentication import supabase, supabase_admin
from app.helpers.account_mail import (
    _forgot_ok,
    _profile_role,
    _public_base_url,
    _send_account_email,
    verify_recovery_token,
)
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
    _email_set_timestamp,
    _record_email_send,
    _report_footer,
    _report_sender_name,
    _saved_address_problem,
    _send_via_resend,
    email_recipient_hash,
    instructor_title,
)
from app.helpers.passwords import password_error
from app.helpers.local_time import local_today
from app.helpers.calendar_dates import clean_assignment_dates
from app.helpers.invites import invite_code_is_valid
from app.helpers.paging import fetch_all_rows
from app.models import (
    Course,
    Grade,
    Homework,
    ASSIGNMENT_TYPES,
    DEFAULT_REQUIRED_MS,
)
from app.helpers.student_names import (
    display_name,
    name_key,
    normalize_email,
    row_sort_key,
)
from app.helpers.lo_import import (
    _insert_learning_objectives,
    _lo_vendor_code_conflict,
    _select_class_pool_learning_objectives,
    annotate_learning_objective_rows_for_preview,
    import_learning_objectives_rows,
)
from app.helpers.enrollment import (
    _classify_student_csv_rows,
    _instructor_student_email_index,
    _profile_used_by_another_instructor,
    _student_csv_rejection,
    decide_student_enrollment,
    validate_student_name_and_email,
)
from app.helpers.class_form import (
    CLASS_DAYS,
    _clean_class_days,
    _clean_class_form,
    _clean_section_number,
    class_display_title,
)
from app.helpers.lo_progress import (
    build_learning_objectives_grid,
    learning_objective_column_name,
)
from app.helpers.revision_report import (
    _revision_view,
    build_revision_report,
)
from app.helpers.csv_text import (
    MAX_IMPORT_ROWS,
    _normalize_assignment_title,
    parse_blank_gradesheet_csv_text,
)
from app.helpers.form_tokens import _consume_one_time_form_token
from app.helpers.auth_decorators import (
    _session_expired_response,
    api_instructor_required,
    api_login_required,
    login_required,
)
from app.helpers.free_passes import (
    FREE_PASSES_ALLOWED,
    _batch_get_free_passes,
    _promote_non_counting_masteries_for_student,
)
from app.helpers.csv_upload import (
    _parse_lo_csv_upload,
    _parse_student_csv_upload,
)
from app.helpers.gradesheet_csv import (
    _build_gradesheet_csv_text,
    _enrolled_import_name_index,
    _gradesheet_csv_data_rows,
    _lookup_enrolled_import_student_id,
)
from app.helpers.class_access import (
    _allowed_lo_ids_for_grading,
    _assignment_belongs_to_class,
    _class_auto_convert_m_enabled,
    _enrolled_student_ids_for_class,
    _instructor_owns_class,
    _owned_class_row,
    _student_enrolled_in_class,
)
from app.helpers.class_progress import (
    _process_enrollments,
    class_lo_lookup,
    class_progress,
    normalize_profile,
    objectives_attached_to_assignments,
)
from app.helpers.class_data import (
    _class_data_for_progress,
    _gradesheet_export_bundle,
    load_assignments_for_class,
    organize_by_learning_objectives,
)
from app.helpers.grade_log import (
    _clear_grade_cells_for_assignment,
    _log_grade_deletions,
    delete_grades_outside_linked_objectives,
    grades_outside_linked_objectives,
)
from app.helpers.class_removal import (
    _filter_query,
    class_student_removal_counts,
    class_student_removal_filters,
    delete_class_rows,
)
from app.helpers.class_archive import (
    _ARCHIVE_ID,
    _RESTORE_FAILED,
    _RESTORE_TABLES,
    _archive_size_label,
    _delete_restore_attempt,
    _owned_archive_row,
    _remove_archive_object,
    _restore_chunks,
    _restore_class_id,
    _restore_error_text,
    _restore_known_message,
    _zip_over_archive_limit,
)
from app.helpers.gradesheet_lookup import _gradesheet_letter_map_for_assignment
from app.helpers.assignment_rows import (
    _assignment_type_fields_from_payload,
    _insert_assignment_objectives,
)
from app.helpers.api_input import (
    _require_json_object,
    _safe_api_error,
)
from app.helpers.history_packet import (
    _HISTORY_GRADE_COLS,
    _HISTORY_LO_CHUNK,
    _HISTORY_PRINT_ERROR,
    _grades_for_this_class,
    _history_objective_ids,
    _history_packet_grades,
    student_history_sheet,
)
from app.helpers.session_status import (
    _session_seconds_left,
    _session_status_response,
)
from app.helpers.rate_limit import rate_limited, _rate_limit, _rate_limit_bucket
from uuid import uuid4

logger = logging.getLogger(__name__)

main_bp = Blueprint('main', __name__, template_folder='templates')

_INSTRUCTOR_HOME = "/instructor/dashboard"


_HW_REQUIRED_FOR_MARK_ERROR = (
    "Enter a homework % for this student before saving a grade other than A (Absent)."
)


# ============================================================================
# AUTHENTICATION ROUTES
# ============================================================================

@main_bp.route("/")
@main_bp.route("/login")
def login_page():
    return render_template("auth/login.html")

@main_bp.route("/signup")
def signup_page():
    return render_template("auth/signup.html")


@main_bp.route("/forgot-password")
def forgot_password_page():
    return render_template("auth/forgot_password.html")


@main_bp.route("/reset-password")
def reset_password_page():
    return render_template("auth/reset_password.html")


@main_bp.route("/logout", methods=["POST"])
def logout():
    from app.helpers.app_sessions import revoke_live_session
    reason = request.form.get("reason")
    try:
        revoke_live_session(session.get("session_token"), session.get("user_id"))
    except Exception as exc:
        logger.error(
            "app_sessions revoke failed user_id=%s: %s",
            session.get("user_id"),
            exc,
        )
    session.clear()
    if reason == "inactivity":
        return redirect(url_for("main.login_page", reason="inactivity"))
    return redirect(url_for("main.login_page"))


@main_bp.route("/api/session-status")
def session_status():
    if "user_id" not in session:
        return _session_status_response({
            "success": False,
            "error": "Unauthorized",
            "code": "session_expired",
        }, 401)
    remaining = _session_seconds_left()
    if remaining is None:
        return _session_status_response({
            "success": False,
            "error": "Unauthorized",
            "code": "session_expired",
        }, 401)
    return _session_status_response({"ok": True, "seconds_left": remaining}, 200)


@main_bp.route("/api/session-keepalive", methods=["POST"])
def session_keepalive():
    if "user_id" not in session:
        return _session_expired_response()
    return jsonify({"ok": True})

@main_bp.route("/api/set-instructor-mode", methods=["POST"])
@api_login_required
def set_instructor_mode():
    data = request.get_json() or {}
    mode = data.get('mode')
    if mode not in ('mark', 'shelbi'):
        return jsonify({"success": False, "error": "Invalid mode"}), 400
    session['instructor_mode'] = mode
    return jsonify({"success": True, "mode": mode})

@main_bp.route("/api/login", methods=["POST"])
@rate_limited("api_login", limit=20, window_sec=300)
def login():
    data = request.get_json()
    try:
        result = supabase.auth.sign_in_with_password({
            "email": data.get("email"), "password": data.get("password")
        })
        if result.user:
            user_id = result.user.id
            try:
                prof_resp = (
                    supabase_admin.table("profiles")
                    .select("role")
                    .eq("id", user_id)
                    .limit(1)
                    .execute()
                )
            except Exception as e:
                logger.error("profiles.role lookup failed for %s: %s", user_id, e)
                return jsonify({"success": False, "message": "Invalid credentials"}), 401

            role = ""
            if prof_resp.data:
                role = (prof_resp.data[0].get("role") or "").strip().lower()
            if role != "instructor":
                return jsonify({"success": False, "message": "Invalid credentials"}), 401

            from app.helpers.app_sessions import (
                absolute_expiry,
                delete_stale_sessions,
                new_session_token,
                remember_app_session,
            )
            token = new_session_token()
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            expires_at = absolute_expiry()
            supabase_admin.table("app_sessions").insert({
                "token_hash": token_hash,
                "user_id": user_id,
                "expires_at": expires_at.isoformat(),
            }).execute()
            remember_app_session(token_hash, user_id, expires_at, time.monotonic())
            session.clear()
            session['user_id'] = user_id
            session['role'] = role
            session['full_name'] = result.user.user_metadata.get('full_name', '')
            session['csrf_token'] = secrets.token_urlsafe(32)
            session['session_token'] = token
            try:
                delete_stale_sessions()
            except Exception as exc:
                logger.error(
                    "app_sessions cleanup failed user_id=%s: %s",
                    user_id,
                    exc,
                )
            return jsonify({"success": True, "redirect": _INSTRUCTOR_HOME})
        return jsonify({"success": False, "message": "Invalid credentials"})
    except Exception as e:
        return _safe_api_error("Invalid credentials", 401, log_detail=e)


@main_bp.route("/api/signup", methods=["POST"])
@rate_limited("api_signup", limit=10, window_sec=300)
def signup():
    data = request.get_json()
    try:
        submitted = (data.get("invite_code") or "").strip()
        if not invite_code_is_valid(submitted):
            return jsonify({"success": False, "message": "Invalid invite code."}), 403

        signup_name = (data.get("name") or "").strip()
        if len(signup_name) > 255:
            return jsonify({"success": False, "message": "Name must be 255 characters or fewer."}), 400

        login_redirect_url = _public_base_url()
        if not login_redirect_url:
            return jsonify({"success": False, "message": "Signup is not available."}), 403
        problem = password_error(data.get("password"), data.get("email"))
        if problem:
            return jsonify({"success": False, "message": problem}), 400
        login_redirect_url = f"{login_redirect_url}/login"
        result = supabase.auth.sign_up({
            "email": data.get("email"),
            "password": data.get("password"),
            "options": {
                "email_redirect_to": login_redirect_url,
                "data": {
                    "full_name": data.get("name"),
                    "invite_code": submitted
                }
            }
        })

        if result.user:
            # Registration succeeds, but account access is blocked until email confirmation.
            # Do not create an authenticated app session at signup time.
            session.clear()
            return jsonify({
                "success": True,
                "requires_email_confirmation": True,
                "message": "Account created. Please confirm your email before signing in.",
            })
        return jsonify({"success": False, "message": "Failed to create account. Please try again."})

    except Exception as e:
        return _safe_api_error("Signup failed. Please try again.", 400, log_detail=e)


@main_bp.route("/api/forgot-password", methods=["POST"])
def forgot_password():
    data, error = _require_json_object()
    if error:
        return error
    if not _rate_limit("forgot_password_ip", 10, 900):
        return jsonify({"success": False, "error": "Rate limit exceeded. Please retry shortly."}), 429
    email = normalize_email((data or {}).get("email"))
    if not email:
        return jsonify({"success": False, "message": "Enter a valid email address."}), 400
    if not _rate_limit_bucket(f"forgot_password_email:{email}", 5, 3600):
        return jsonify({"success": False, "error": "Rate limit exceeded. Please retry shortly."}), 429
    base = _public_base_url()
    if not base:
        return jsonify({"success": False, "message": "Password reset is not available."}), 403
    try:
        link = supabase_admin.auth.admin.generate_link({
            "type": "recovery",
            "email": email,
            "options": {"redirect_to": f"{base}/reset-password"},
        })
    except Exception as exc:
        logger.error("Password reset link failed: %s", type(exc).__name__)
        return _forgot_ok()
    user = getattr(link, "user", None)
    user_id = getattr(user, "id", None) if user is not None else None
    properties = getattr(link, "properties", None)
    hashed = getattr(properties, "hashed_token", None) if properties is not None else None
    if not user_id or not hashed:
        return _forgot_ok()
    if _profile_role(user_id) != "instructor":
        return _forgot_ok()
    if not _rate_limit_bucket(f"forgot_password_account:{user_id}", 3, 3600):
        return _forgot_ok()
    token = quote(str(hashed), safe="")
    mailed = quote(email, safe="")
    url = f"{base}/reset-password#token_hash={token}&type=recovery&email={mailed}"
    _send_account_email(
        email,
        "Reset your Clarity Grader password",
        "Use this link to choose a new Clarity Grader password. It expires in one hour.\n\n"
        f"{url}\n\n"
        "If you did not ask for this, you can ignore this message.\n",
    )
    return _forgot_ok()


@main_bp.route("/api/reset-password", methods=["POST"])
def reset_password():
    data, error = _require_json_object()
    if error:
        return error
    email = normalize_email((data or {}).get("email"))
    token_hash = str((data or {}).get("token_hash") or "").strip()
    password = (data or {}).get("password")
    if not email or not token_hash or not isinstance(password, str):
        return jsonify({"success": False, "message": "This reset link is invalid or expired."}), 400
    problem = password_error(password, email)
    if problem:
        return jsonify({"success": False, "message": problem}), 400
    if not _rate_limit("reset_password_ip", 10, 900):
        return jsonify({"success": False, "error": "Rate limit exceeded. Please retry shortly."}), 429
    try:
        verified = verify_recovery_token(token_hash)
    except Exception as exc:
        logger.error("Password reset verify failed: %s", type(exc).__name__)
        return jsonify({"success": False, "message": "This reset link is invalid or expired."}), 400
    user = getattr(verified, "user", None)
    user_id = getattr(user, "id", None) if user is not None else None
    verified_email = normalize_email(
        getattr(user, "email", None) if user is not None else None
    )
    if not user_id or verified_email != email or _profile_role(user_id) != "instructor":
        return jsonify({"success": False, "message": "This reset link is invalid or expired."}), 400
    try:
        from app.helpers.app_sessions import revoke_user_sessions
        revoke_user_sessions(user_id)
    except Exception as exc:
        logger.error("Password reset session revoke failed: %s", type(exc).__name__)
        return jsonify({
            "success": False,
            "message": "We couldn't save the new password. Request a new reset link.",
        }), 500
    try:
        supabase_admin.auth.admin.update_user_by_id(user_id, {"password": password})
    except Exception as exc:
        logger.error("Password reset update failed: %s", type(exc).__name__)
        return jsonify({
            "success": False,
            "message": "We couldn't save the new password. Request a new reset link.",
        }), 400
    sent = _send_account_email(
        email,
        "Your Clarity Grader password was changed",
        "Your Clarity Grader password was changed.\n\n"
        "If you did not make this change, contact the site owner.\n",
    )
    if not sent:
        logger.error("Password changed notice failed")
    return jsonify({"success": True, "redirect": "/login?reset=1"})


# ============================================================================
# DASHBOARD ROUTES
# ============================================================================

@main_bp.route("/instructor/dashboard")
@login_required
def instructor_dashboard():
    if session.get('role') != 'instructor':
        return redirect(url_for('main.login_page'))
    db_classes = Course.get_all_for_instructor(session['user_id'])
    create_class_token = str(uuid4())
    session['create_class_token'] = create_class_token
    copy_class_token = str(uuid4())
    session['copy_class_token'] = copy_class_token
    return render_template(
        "classes/instructor_select_class.html",
        classes=db_classes,
        create_class_token=create_class_token,
        copy_class_token=copy_class_token,
        class_days=CLASS_DAYS,
    )

# ============================================================================
# CLASS MANAGEMENT ROUTES
# ============================================================================

_REVISION_EMAIL_BUDGET_SECONDS = 60
_REVISION_EMAIL_PAUSE_SECONDS = 0.5
_REVISION_FOOTER = (
    "Replies to this email are not monitored. "
    "Contact your instructor directly."
)
_REVISION_SCORE_PHRASE = {
    "R": "Revise for Mastery",
    "RQ": "Revise with Question (RQ)",
}


def _revision_due_phrase(iso_date: str) -> str:
    year, month, day = [int(part) for part in str(iso_date).split("-")]
    written = date(year, month, day)
    return written.strftime("%A, %b ") + str(written.day)


def _revision_greeting(student_name: str) -> str:
    text = " ".join(str(student_name or "").split())
    if not text:
        return "student"
    if "," not in text:
        return text
    given = " ".join(text.split(",", 1)[1].split())
    return given or text


def _revision_signoff(full_name: str) -> str:
    """Natural order. Signup stores the typed name, often Last, First."""
    text = " ".join(str(full_name or "").split())
    if not text:
        return "Your instructor"
    if "," not in text:
        return text
    last, given = text.split(",", 1)
    last = " ".join(last.split())
    given = " ".join(given.split())
    if given and last:
        return f"{given} {last}"
    return given or last or "Your instructor"


def _revision_objective_line(obj: Dict[str, Any]) -> str:
    parts = []
    code = str(obj.get("lo_code") or "").strip()
    if code:
        parts.append(code)
    if obj.get("show_name"):
        parts.append(str(obj.get("lo_name") or "").strip())
    label = " ".join(part for part in parts if part) or "Objective"
    mark = str(obj.get("top_score") or "").strip()
    return f"{label}: {_REVISION_SCORE_PHRASE.get(mark, mark)}"


def _revision_message(
    student: Dict[str, Any], due_iso: str, class_title: str, signature: str
) -> str:
    lines = [
        f"Hello {_revision_greeting(student.get('name') or '')},",
        "",
        f"You have a revision due in 3 days, on {_revision_due_phrase(due_iso)}.",
        f"Class: {class_title}",
        "",
        "Please revise the following:",
        "",
    ]
    assignments = list(student.get("assignments") or [])
    for index, assignment in enumerate(assignments):
        if index:
            lines.append("")
        name = str(assignment.get("assignment_name") or "Assignment")
        lines.append(f"Assignment: {name}")
        for obj in assignment.get("objectives") or []:
            lines.append("- " + _revision_objective_line(obj))
    lines.extend(["", "Thank you,", signature, "", _REVISION_FOOTER])
    return "\n".join(lines)


def _revision_named_rows(class_id, today):
    class_data = Course.get_full_class_data(class_id)
    if not class_data:
        return [], []
    progress = _class_data_for_progress(class_data)
    active, _, _ = _process_enrollments(progress)
    names = {
        student["id"]: display_name(student.get("full_name") or "")
        for student in active
    }
    named = []
    for row in Grade.get_revision_candidates(class_id, today):
        if row.get("student_id") in names:
            row["student_name"] = names[row["student_id"]]
            named.append(row)
    return named, active


def _load_sent_reminder_keys(class_id: str, due_iso: str):
    resp = (
        supabase_admin.table("revision_reminders_sent")
        .select("student_id, assignment_id, learning_objective_id")
        .eq("class_id", class_id)
        .eq("revision_due", due_iso)
        .execute()
    )
    keys = set()
    for row in resp.data or []:
        keys.add((
            str(row.get("student_id") or ""),
            str(row.get("assignment_id") or ""),
            str(row.get("learning_objective_id") or ""),
        ))
    return keys


def revision_reminder_partition(class_id, today, named_rows=None, students=None):
    """Recipients, students with no usable email, and students already reminded.

    The reminders table is read once. Pass named_rows and students from a page
    that already loaded them. Preview and send omit both and load them here.
    """
    due_iso = (today + timedelta(days=3)).isoformat()
    if named_rows is None or students is None:
        named_rows, students = _revision_named_rows(class_id, today)
    email_by_id = {}
    set_at_by_id = {}
    for student in students or []:
        sid = str(student.get("id") or "")
        email_by_id[sid] = str(student.get("email") or "").strip()
        set_at_by_id[sid] = student.get("email_set_at")
    sent = _load_sent_reminder_keys(class_id, due_iso)
    due_rows = [
        row for row in (named_rows or [])
        if (row.get("revision_due") or "") == due_iso
    ]
    open_rows = []
    for row in due_rows:
        key = (
            str(row.get("student_id") or ""),
            str(row.get("assignment_id") or ""),
            str(row.get("learning_objective_id") or ""),
        )
        if key not in sent:
            open_rows.append(row)
    everyone = _revision_view("3", "", "", "", due_rows)["students"]
    still_open = {
        str(student["student_id"]): student
        for student in _revision_view("3", "", "", "", open_rows)["students"]
    }
    recipients = []
    no_email = []
    invalid = []
    held = []
    already_reminded = []
    for student in everyone:
        sid = str(student["student_id"])
        if sid not in still_open:
            already_reminded.append({
                "student_id": sid,
                "name": student["student_name"],
            })
            continue
        email, kind = _saved_address_problem(
            email_by_id.get(sid, ""),
            set_at_by_id.get(sid),
        )
        if kind == "invalid":
            invalid.append({
                "student_id": sid,
                "name": student["student_name"],
                "message": _ADDRESS_KINDS["invalid"][0],
            })
            continue
        if kind == "missing":
            no_email.append({
                "student_id": sid,
                "name": student["student_name"],
            })
            continue
        if kind == "held":
            held.append({
                "student_id": sid,
                "name": student["student_name"],
                "message": _ADDRESS_KINDS["held"][0],
            })
            continue
        recipients.append({
            "student_id": sid,
            "name": student["student_name"],
            "email": email,
            "assignments": still_open[sid]["assignments"],
        })
    return {
        "due": due_iso,
        "recipients": recipients,
        "no_email": no_email,
        "invalid": invalid,
        "held": held,
        "already_reminded": already_reminded,
    }


def _revision_sender_name() -> str:
    return _revision_signoff(str(session.get("full_name") or ""))


def _attach_revision_messages(partition, class_title, signature):
    subject = f"Revision due in 3 days: {class_title}"
    for student in partition["recipients"]:
        student["subject"] = subject
        student["body"] = _revision_message(
            student, partition["due"], class_title, signature
        )
    return subject


def _insert_reminder_rows(class_id, student, due_iso, sent_by):
    rows = []
    for assignment in student.get("assignments") or []:
        for obj in assignment.get("objectives") or []:
            rows.append({
                "class_id": class_id,
                "student_id": student["student_id"],
                "assignment_id": assignment["assignment_id"],
                "learning_objective_id": obj["learning_objective_id"],
                "revision_due": due_iso,
                "sent_by": sent_by,
            })
    if rows:
        supabase_admin.table("revision_reminders_sent").upsert(
            rows,
            on_conflict="student_id,assignment_id,learning_objective_id,revision_due",
            ignore_duplicates=True,
        ).execute()


def _send_revision_reminders(class_id, recipients, due_iso, sender_name, sent_by):
    hashes = [
        email_recipient_hash(student["email"])
        for student in recipients
        if student.get("email")
    ]
    instructor_count = 0
    per_hash = {}
    if hashes:
        loaded = _email_cap_counts(sent_by, hashes)
        if loaded is None:
            return None
        instructor_count, per_hash = loaded
    deadline = time.monotonic() + _REVISION_EMAIL_BUDGET_SECONDS
    results = []
    attempted = 0
    sent_now = 0
    hash_sent = {}
    for index, student in enumerate(recipients):
        if index >= EMAIL_MAX_RECIPIENTS or time.monotonic() >= deadline:
            break
        if index:
            time.sleep(_REVISION_EMAIL_PAUSE_SECONDS)
            if time.monotonic() >= deadline:
                break
        digest = email_recipient_hash(student["email"])
        reason = _cap_block_reason(
            instructor_count + sent_now,
            per_hash.get(digest, 0) + hash_sent.get(digest, 0),
        )
        if reason:
            results.append({
                "student_id": student["student_id"],
                "name": student["name"],
                "status": "capped",
                "message": reason,
            })
            attempted = index + 1
            continue
        ok, _err = _send_via_resend(
            student["email"],
            student["subject"],
            student["body"],
            sender_name=sender_name,
        )
        if not ok:
            results.append({
                "student_id": student["student_id"],
                "name": student["name"],
                "status": "failed",
            })
            attempted = index + 1
            continue
        _record_email_send(
            sent_by, class_id, student["student_id"], digest, "revision"
        )
        sent_now += 1
        hash_sent[digest] = hash_sent.get(digest, 0) + 1
        try:
            _insert_reminder_rows(class_id, student, due_iso, sent_by)
        except Exception:
            logger.error(
                "Revision reminder accepted but not recorded for class %s student %s",
                class_id,
                student["student_id"],
            )
            results.append({
                "student_id": student["student_id"],
                "name": student["name"],
                "status": "sent_not_recorded",
            })
            attempted = index + 1
            continue
        results.append({
            "student_id": student["student_id"],
            "name": student["name"],
            "status": "sent",
        })
        attempted = index + 1
    for student in recipients[attempted:]:
        results.append({
            "student_id": student["student_id"],
            "name": student["name"],
            "status": "not_attempted",
        })
    return results


def _reminder_counts(results):
    counts = {
        "sent": 0,
        "failed": 0,
        "skipped": 0,
        "not_attempted": 0,
        "sent_not_recorded": 0,
        "held": 0,
        "invalid": 0,
        "capped": 0,
    }
    for row in results:
        status = row["status"]
        if status in counts:
            counts[status] += 1
    return counts


@main_bp.route("/class/<class_id>")
@login_required
def class_detail(class_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))
    class_data = Course.get_full_class_data(class_id)
    if not class_data:
        logger.error("class_detail: get_full_class_data returned None for class_id=%s", class_id)
        return redirect(url_for('main.instructor_dashboard'))

    progress_data = _class_data_for_progress(class_data)
    students_for_template, all_students_for_modal, _ = _process_enrollments(progress_data)
    summary = organize_by_learning_objectives(
        students_for_template, progress_data.get("learning_objectives") or []
    )
    assignments = load_assignments_for_class(class_id)

    today = local_today()
    pool_los = class_data.get("learning_objectives") or []
    revision_raw = Grade.get_revision_candidates(
        class_id,
        today,
        [str(lo.get("id")) for lo in pool_los if lo.get("id")],
    )
    student_name_map = {
        s["id"]: display_name(s.get("full_name") or "")
        for s in students_for_template
    }
    named_revisions = []
    for rev in revision_raw:
        if rev['student_id'] in student_name_map:
            rev['student_name'] = student_name_map[rev['student_id']]
            named_revisions.append(rev)
    revision_report = build_revision_report(named_revisions, today)

    revision_unsent_count = None
    revision_email_note = ""
    try:
        reminder_partition = revision_reminder_partition(
            class_id,
            today,
            named_rows=named_revisions,
            students=students_for_template,
        )
    except Exception as e:
        logger.error("Revision reminder lookup failed for class %s: %s", class_id, e)
    else:
        revision_unsent_count = len(reminder_partition["recipients"])
        day3 = next(view for view in revision_report["views"] if view["key"] == "3")
        unusable = reminder_partition["no_email"] or reminder_partition.get("invalid")
        if revision_unsent_count == 0 and day3["students"]:
            if reminder_partition.get("held") and not unusable and not reminder_partition["already_reminded"]:
                revision_email_note = EMAIL_HOLD_MESSAGE
            elif unusable and not reminder_partition["already_reminded"]:
                revision_email_note = "No students have a usable email address."
            else:
                revision_email_note = "All students have been emailed."

    return render_template('classes/class_detail.html', 
                            class_id=class_id, 
                            class_name=class_data.get('name'),
                            class_days=class_data.get("days"),
                            class_section=class_data.get("section_number"), 
                            students=students_for_template, 
                            all_students=all_students_for_modal,
                            learning_objectives=summary,
                            assignments=assignments,
                            revision_report=revision_report,
                            revision_printed_on=today.isoformat(),
                            revision_unsent_count=revision_unsent_count,
                            revision_email_note=revision_email_note)


@main_bp.route("/class/<class_id>/learning-objectives")
@login_required
def class_learning_objectives_summary(class_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))

    class_data = Course.get_full_class_data(class_id)
    if not class_data:
        logger.error("class_learning_objectives_summary: get_full_class_data returned None for class_id=%s", class_id)
        return redirect(url_for('main.instructor_dashboard'))

    progress_data = _class_data_for_progress(class_data)
    students_for_template, _, _ = _process_enrollments(progress_data)
    class_los = progress_data.get("learning_objectives") or []
    total_students = len(students_for_template)

    summary_rows = []
    for lo in class_los:
        lo_id = str(lo.get("id") or "")
        passed_count = 0
        for student in students_for_template:
            student_los = student.get("learning_objectives", []) or []
            matched = next(
                (slo for slo in student_los if str(slo.get("learning_objective_id") or "") == lo_id),
                None
            )
            if matched and bool(matched.get("is_passed")):
                passed_count += 1
        summary_rows.append({
            "id": lo_id,
            "vendor_code": learning_objective_column_name(lo),
            "name": lo.get("name") or lo.get("vendor_code") or "Objective",
            "required_ms": lo.get("required_ms", 2),
            "passed_count": passed_count,
        })

    grid_rows = build_learning_objectives_grid(students_for_template, summary_rows)

    return render_template(
        "classes/class_learning_objectives_summary.html",
        class_id=class_id,
        class_name=class_data.get("name"),
        class_days=class_data.get("days"),
        class_section=class_data.get("section_number"),
        class_semester=class_data.get("semester"),
        printed_on=date.today().isoformat(),
        learning_objectives=summary_rows,
        total_students=total_students,
        grid_rows=grid_rows,
    )


@main_bp.route("/class/<class_id>/add_student", methods=["POST"])
@api_instructor_required
def add_student(class_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    data = request.get_json() or {}
    raw_email = data.get("email") or ""
    raw_name = data.get("name") or ""

    name, email, name_error = validate_student_name_and_email(raw_name, raw_email)
    if name_error:
        return jsonify({"success": False, "error": name_error}), 400

    owner_id = session.get("user_id")
    try:
        index = _instructor_student_email_index(owner_id)
        outcome, stored_name, profile_id = decide_student_enrollment(
            index, class_id, email, name
        )
        if outcome == "already_in_class":
            return jsonify({
                "success": False,
                "error": "A student with that email is already in this class",
            }), 409
        if outcome == "name_differs":
            return jsonify({
                "success": False,
                "error": (
                    f"That email belongs to {stored_name}. "
                    f"Enter the name as {stored_name} to add them to this class."
                ),
            }), 409
        if outcome == "reuse":
            supabase_admin.table("enrollments").insert({
                "class_id": class_id,
                "student_id": profile_id,
            }).execute()
            return jsonify({
                "success": True,
                "full_name": stored_name,
            })
        student_id = str(uuid4())
        insert_row = {
            "id": student_id,
            "full_name": name,
            "role": "student",
            "email": email,
            "email_set_at": _email_set_timestamp(),
        }
        supabase_admin.table("profiles").insert(insert_row).execute()
        supabase_admin.table("enrollments").insert({
            "class_id": class_id,
            "student_id": student_id
        }).execute()
        return jsonify({
            "success": True,
            "full_name": name,
        })
    except Exception as e:
        return _safe_api_error("Could not add student", 500, log_detail=e)


@main_bp.route("/class/<class_id>/students/<student_id>/delete", methods=["POST"])
@api_instructor_required
def delete_student_from_class(class_id, student_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    if not _student_enrolled_in_class(class_id, student_id):
        return jsonify({"success": False, "error": "Student not enrolled in this class"}), 403
    try:
        filters = class_student_removal_filters(class_id, student_id)
        grade_scope = next((scope for table, scope in filters if table == "grades"), None)
        if grade_scope:
            audit = (
                supabase_admin.table("grades")
                .select(
                    "student_id, learning_objective_id, assignment_id, top_score, "
                    "second_score, counts_for_mastery"
                )
                .order("id")
            )
            existing = fetch_all_rows(_filter_query(audit, grade_scope))
            _log_grade_deletions(existing, class_id, session["user_id"])
        for table, scope in filters:
            _filter_query(supabase_admin.table(table).delete(), scope).execute()
        return jsonify({"success": True})
    except Exception as e:
        logger.error("Error deleting student %s from class %s: %s", student_id, class_id, e)
        return _safe_api_error("Could not delete student", 500, log_detail=e)


@main_bp.route("/api/class/<class_id>/students/<student_id>/removal-counts", methods=["GET"])
@api_instructor_required
def api_student_removal_counts(class_id, student_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    if not _student_enrolled_in_class(class_id, student_id):
        return jsonify({"success": False, "error": "Student not enrolled in this class"}), 403
    counts = class_student_removal_counts(class_id, student_id)
    return jsonify({
        "success": True,
        "grade_rows": counts["grades"],
        "homework_scores": counts["homework_scores"],
    })


@main_bp.route("/api/class/<class_id>/students/<student_id>/update", methods=["POST"])
@api_instructor_required
def api_update_student(class_id, student_id):
    """Update a student's name and, when an email is sent, that email.

    Ownership is verified two ways:
    1. The instructor must own the class (via _instructor_owns_class).
    2. The student must be enrolled in that class (via _student_enrolled_in_class),
       preventing an instructor from editing a stranger's profile using a
       student_id they guessed.
    Email is required, lowercased, and stored. Another profile in this
    instructor's classes cannot already have that email.
    """
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    if not _student_enrolled_in_class(class_id, student_id):
        return jsonify({"success": False, "error": "Student not enrolled in this class"}), 403

    data = request.get_json() or {}
    raw_name = data.get("name") or ""
    raw_email = data.get("email") or ""

    name, email, name_error = validate_student_name_and_email(raw_name, raw_email)
    if name_error:
        return jsonify({"success": False, "error": name_error}), 400

    owner_id = session.get("user_id")
    try:
        index = _instructor_student_email_index(owner_id)
        entry = index.get(email)
        if entry and str(entry.get("profile_id")) != str(student_id):
            return jsonify({
                "success": False,
                "error": "That email already belongs to another student in your classes",
            }), 409
        if _profile_used_by_another_instructor(student_id, owner_id):
            return jsonify({
                "success": False,
                "error": (
                    "This student is enrolled in another instructor's class. "
                    "Nothing was changed."
                ),
            }), 409
        current = (
            supabase_admin.table("profiles")
            .select("email")
            .eq("id", student_id)
            .limit(1)
            .execute()
        )
        stored_raw = ""
        rows = getattr(current, "data", None)
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            stored_raw = rows[0].get("email") or ""
        if not isinstance(stored_raw, str):
            stored_raw = ""
        changes = {"full_name": name, "email": email}
        if normalize_email(stored_raw) != email:
            changes["email_set_at"] = _email_set_timestamp()
        supabase_admin.table("profiles").update(changes).eq("id", student_id).execute()
        logger.info(
            "roster edit user_id=%s class_id=%s",
            owner_id,
            class_id,
        )
        return jsonify({"success": True})
    except Exception as e:
        return _safe_api_error("Could not update student", 500, log_detail=e)


@main_bp.route("/class/<class_id>/students")
@login_required
def class_students(class_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))
    class_data = Course.get_full_class_data(class_id)
    
    if not class_data:
        return redirect(url_for('main.instructor_dashboard'))

    students, _, _ = _process_enrollments(_class_data_for_progress(class_data))

    return render_template("classes/class_students.html",
                            class_id=class_id,
                            class_name=class_data['name'],
                            class_days=class_data.get("days"),
                            class_section=class_data.get("section_number"),
                            students=students)

@main_bp.route("/class/<class_id>/delete", methods=["POST"])
@api_instructor_required
def delete_class(class_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    try:
        delete_class_rows(class_id, session["user_id"])
        return redirect(url_for("main.instructor_dashboard"))
    except Exception as e:
        logger.error("Error deleting class %s: %s", class_id, e)
        return _safe_api_error("Could not delete class", 500, log_detail=e)


@main_bp.route("/class/<class_id>/archive", methods=["POST"])
@api_instructor_required
@rate_limited("archive_create", limit=5, window_sec=3600)
def archive_class(class_id):
    from app.helpers.archive_export import build_class_archive_zip, check_archive_manifest
    from app.helpers.archive_snapshot import build_class_snapshot
    from app.helpers.archive_storage import (
        download_archive,
        remove_archive,
        upload_archive,
        upload_snapshot,
    )

    kept_error = (
        "The class was archived, but it was not fully removed. "
        "The archive is saved under Archived Classes. "
        "Delete the class to finish removing it."
    )
    budget_error = (
        "The archive took too long, or the class changed. Nothing was removed. Try again."
    )
    instructor_id = session["user_id"]
    class_row = _owned_class_row(class_id, instructor_id)
    if class_row is None:
        return jsonify({
            "success": False,
            "error": "This class was not found. It may already be archived.",
        }), 404
    if request.form.get("confirm_name") != class_row.get("name"):
        return jsonify({
            "success": False,
            "error": "Type the class name exactly to archive it.",
        }), 400

    archive_id = str(uuid4())
    path = f"{instructor_id}/{archive_id}.zip"
    snapshot_path = f"{instructor_id}/{archive_id}.snapshot.json.gz"
    start = time.monotonic()

    def discard(message):
        for object_path in (snapshot_path, path):
            try:
                remove_archive(object_path)
            except Exception:
                logger.error("Could not remove archive at %s", object_path)
        return jsonify({"success": False, "error": message}), 500

    try:
        zip_bytes, manifest = build_class_archive_zip(class_id)
        check_archive_manifest(manifest, zip_bytes)
    except Exception as exc:
        logger.error("Could not build archive for class %s: %s", class_id, exc)
        return jsonify({
            "success": False,
            "error": "Could not build the archive. Nothing was changed.",
        }), 500
    too_big = _zip_over_archive_limit(zip_bytes)
    if too_big is not None:
        return too_big
    try:
        snapshot_bytes = build_class_snapshot(class_id, manifest)
    except Exception as exc:
        logger.error("Could not build snapshot for class %s: %s", class_id, exc)
        return jsonify({
            "success": False,
            "error": "Could not build the archive. Nothing was changed.",
        }), 500
    if time.monotonic() - start > 200:
        return jsonify({"success": False, "error": budget_error}), 500
    try:
        upload_archive(path, zip_bytes)
    except Exception as exc:
        logger.error("Could not upload archive %s: %s", path, exc)
        return discard("Could not save the archive. The class was not changed.")
    try:
        upload_snapshot(snapshot_path, snapshot_bytes)
    except Exception as exc:
        logger.error("Could not upload snapshot %s: %s", snapshot_path, exc)
        return discard("Could not save the archive. The class was not changed.")
    try:
        saved = download_archive(path)
        mismatch = saved != zip_bytes
        check_archive_manifest(manifest, saved)
        if mismatch:
            raise ValueError("downloaded archive bytes do not match the upload")
        del saved
        saved_snapshot = download_archive(snapshot_path)
        if saved_snapshot != snapshot_bytes:
            raise ValueError("downloaded snapshot bytes do not match the upload")
        del saved_snapshot
        size_bytes = len(zip_bytes)
        del zip_bytes
        del snapshot_bytes
    except Exception as exc:
        logger.error("Archive %s failed verification: %s", path, exc)
        return discard(
            "The saved archive did not verify and was removed. The class was not changed."
        )
    try:
        again = _owned_class_row(class_id, instructor_id)
    except Exception as exc:
        logger.error("Could not re-check class %s before recording the archive: %s", class_id, exc)
        return discard(budget_error)
    if again is None or time.monotonic() - start > 200:
        return discard(budget_error)
    record = {
        "id": archive_id,
        "instructor_id": instructor_id,
        "class_name": again.get("name"),
        "semester": again.get("semester"),
        "days": again.get("days"),
        "section_number": again.get("section_number"),
        "student_count": manifest.get("source_students"),
        "assignment_count": manifest.get("source_assignments"),
        "storage_path": path,
        "snapshot_path": snapshot_path,
        "size_bytes": size_bytes,
    }
    try:
        supabase_admin.table("archived_classes").insert(record).execute()
        logger.info(
            "archive create user_id=%s class_id=%s",
            instructor_id,
            class_id,
        )
    except Exception as exc:
        logger.error("Could not record archive %s: %s", archive_id, exc)
        try:
            supabase_admin.table("archived_classes").delete().eq("id", archive_id).execute()
        except Exception as delete_exc:
            logger.error("Could not delete archive row %s: %s", archive_id, delete_exc)
        return discard("Could not record the archive. The class was not changed.")
    try:
        still_owned = _owned_class_row(class_id, instructor_id)
    except Exception as exc:
        logger.error(
            "Could not re-check class %s after recording archive %s: %s",
            class_id, archive_id, exc,
        )
        return jsonify({"success": False, "archived": True, "error": kept_error}), 500
    if still_owned is None:
        return jsonify({
            "success": True,
            "redirect": url_for("main.instructor_dashboard"),
        })
    try:
        delete_class_rows(class_id, instructor_id)
    except Exception as exc:
        logger.error(
            "Archive %s saved but class %s was not fully removed: %s",
            archive_id, class_id, exc,
        )
        return jsonify({"success": False, "archived": True, "error": kept_error}), 500
    return jsonify({
        "success": True,
        "redirect": url_for("main.instructor_dashboard"),
    })


@main_bp.route("/archived-classes")
@login_required
def archived_classes():
    if session.get("role") != "instructor":
        return redirect(url_for("main.login_page"))
    instructor_id = session["user_id"]
    rows = fetch_all_rows(
        supabase_admin.table("archived_classes")
        .select(
            "id, class_name, semester, days, section_number, "
            "student_count, assignment_count, size_bytes, archived_at, "
            "snapshot_path, restored_at, restored_class_id"
        )
        .eq("instructor_id", instructor_id)
        .order("archived_at", desc=True)
        .order("id", desc=True)
    )
    archives = []
    for row in rows:
        parts = [row.get("semester"), row.get("days"), row.get("section_number")]
        snapshot_path = str(row.get("snapshot_path") or "").strip()
        restored_at = row.get("restored_at") or ""
        restored_class_id = str(row.get("restored_class_id") or "").strip()
        archives.append({
            "id": row.get("id"),
            "class_name": row.get("class_name") or "",
            "detail": " · ".join(str(part) for part in parts if part),
            "student_count": row.get("student_count") or 0,
            "assignment_count": row.get("assignment_count") or 0,
            "archived_at": row.get("archived_at") or "",
            "size_label": _archive_size_label(row.get("size_bytes")),
            "restored": bool(restored_at),
            "restorable": bool(snapshot_path) and not restored_at,
            "restored_at": restored_at,
            "restored_class_id": restored_class_id if restored_at else "",
        })
    return render_template("classes/archived_classes.html", archives=archives)


@main_bp.route("/archived-classes/<archive_id>/download")
@login_required
@rate_limited("archive_download", limit=30, window_sec=3600)
def download_archived_class(archive_id):
    if session.get("role") != "instructor":
        return redirect(url_for("main.login_page"))
    from app.helpers.archive_storage import download_archive
    row = _owned_archive_row(archive_id, session["user_id"])
    if row is None:
        return jsonify({"success": False, "error": "This archive was not found."}), 404
    try:
        payload = download_archive(row.get("storage_path") or "")
    except Exception as exc:
        logger.error("Could not download archive %s: %s", archive_id, exc)
        return _safe_api_error("Could not download the archive", 500, log_detail=exc)
    filename = f"{secure_filename(str(row.get('class_name') or '')) or 'class'}_archive.zip"
    logger.info(
        "archive download user_id=%s archive_id=%s",
        session.get("user_id"),
        archive_id,
    )
    return Response(
        payload,
        mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@main_bp.route("/archived-classes/<archive_id>/delete", methods=["POST"])
@api_instructor_required
def delete_archived_class(archive_id):
    if _ARCHIVE_ID.fullmatch(str(archive_id or "")) is None:
        return jsonify({"success": False, "error": "This archive was not found."}), 404
    instructor_id = session["user_id"]
    row = _owned_archive_row(archive_id, instructor_id)
    if row is None:
        return jsonify({
            "success": True,
            "redirect": url_for("main.archived_classes"),
        })
    if request.form.get("confirm_name") != row.get("class_name"):
        return jsonify({
            "success": False,
            "error": "Type the class name exactly to delete this archive.",
        }), 400
    try:
        _remove_archive_object(row.get("storage_path") or "")
        _remove_archive_object(row.get("snapshot_path") or "")
    except Exception as exc:
        logger.error("Could not remove archive %s: %s", archive_id, exc)
        return jsonify({
            "success": False,
            "error": "Could not delete the archive file. The record was kept.",
        }), 500
    try:
        supabase_admin.table("archived_classes").delete().eq("id", archive_id).eq(
            "instructor_id", instructor_id
        ).execute()
    except Exception as exc:
        logger.error(
            "Could not delete archive row %s after the file was removed: %s",
            archive_id, exc,
        )
        return jsonify({
            "success": False,
            "error": "The file was deleted but the record could not be removed. Try again.",
        }), 500
    return jsonify({
        "success": True,
        "redirect": url_for("main.archived_classes"),
    })


@main_bp.route("/archived-classes/<archive_id>/restore", methods=["POST"])
@api_instructor_required
@rate_limited("archive_restore", limit=5, window_sec=3600)
def restore_archived_class(archive_id):
    import json
    from app.helpers.archive_snapshot import GzipTooLarge, gunzip_bounded
    from app.helpers.archive_storage import download_archive

    start = time.monotonic()
    if _ARCHIVE_ID.fullmatch(str(archive_id or "")) is None:
        return jsonify({"success": False, "error": "This archive was not found."}), 404
    instructor_id = session["user_id"]
    row = _owned_archive_row(archive_id, instructor_id)
    if row is None:
        return jsonify({"success": False, "error": "This archive was not found."}), 404
    if not str(row.get("snapshot_path") or "").strip():
        return jsonify({
            "success": False,
            "error": "This archive is not restorable.",
        }), 409
    if row.get("restored_at"):
        return jsonify({
            "success": False,
            "error": "This archive has already been restored.",
        }), 409

    try:
        blob = download_archive(row.get("snapshot_path") or "")
        try:
            raw = gunzip_bounded(blob)
        finally:
            del blob
    except GzipTooLarge:
        return jsonify({
            "success": False,
            "error": "This archive is too large to restore. Nothing was changed.",
        }), 413
    except Exception as exc:
        logger.error("Could not read snapshot for archive %s: %s", archive_id, exc)
        return jsonify({"success": False, "error": _RESTORE_FAILED}), 500
    try:
        document = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        logger.error("Could not read snapshot for archive %s: %s", archive_id, exc)
        return jsonify({"success": False, "error": _RESTORE_FAILED}), 500
    finally:
        del raw
    if not isinstance(document, dict) or document.get("format_version") != 1:
        return jsonify({
            "success": False,
            "error": "This archive is not restorable.",
        }), 409

    loaded = {}
    for table_name in _RESTORE_TABLES:
        rows = document.pop(table_name, [])
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            return jsonify({
                "success": False,
                "error": "This archive is not restorable.",
            }), 409
        loaded[table_name] = rows
    del document

    attempt_id = str(uuid4())
    staged = False
    counts = {}
    try:
        for table_name in _RESTORE_TABLES:
            rows = loaded.pop(table_name)
            records = [
                {
                    "attempt_id": attempt_id,
                    "table_name": table_name,
                    "seq": index,
                    "row_data": item,
                }
                for index, item in enumerate(rows, start=1)
            ]
            counts[table_name] = len(records)
            del rows
            for chunk in _restore_chunks(records):
                if time.monotonic() - start > 240:
                    _delete_restore_attempt(attempt_id)
                    return jsonify({
                        "success": False,
                        "error": "The restore took too long. Nothing was changed.",
                    }), 500
                staged = True
                supabase_admin.table("archive_restore_stage").insert(chunk).execute()
            del records
        if time.monotonic() - start > 240:
            _delete_restore_attempt(attempt_id)
            return jsonify({
                "success": False,
                "error": "The restore took too long. Nothing was changed.",
            }), 500
        result = supabase_admin.rpc(
            "restore_archived_class",
            {
                "p_archive_id": archive_id,
                "p_instructor_id": instructor_id,
                "p_attempt_id": attempt_id,
                "p_expected": counts,
            },
        ).execute()
        class_id = _restore_class_id(result.data)
        if class_id is None:
            logger.error(
                "Restore of archive %s returned no class id: %s",
                archive_id,
                result.data,
            )
            return jsonify({
                "success": True,
                "redirect": url_for("main.instructor_dashboard"),
            })
    except Exception as exc:
        if staged:
            _delete_restore_attempt(attempt_id)
        known = _restore_known_message(_restore_error_text(exc))
        if known:
            return jsonify({"success": False, "error": known}), 409
        logger.error("Could not restore archive %s: %s", archive_id, exc)
        return jsonify({"success": False, "error": _RESTORE_FAILED}), 500
    try:
        _remove_archive_object(row.get("snapshot_path") or "")
        _remove_archive_object(row.get("storage_path") or "")
        supabase_admin.table("archived_classes").delete().eq(
            "id", archive_id
        ).eq("instructor_id", instructor_id).execute()
    except Exception as exc:
        logger.error("Could not remove restored archive %s: %s", archive_id, exc)
    logger.info(
        "archive restore user_id=%s class_id=%s",
        instructor_id,
        class_id,
    )
    return jsonify({
        "success": True,
        "redirect": url_for("main.class_detail", class_id=class_id),
    })


@main_bp.route("/class/<class_id>/update", methods=["POST"])
@api_instructor_required
def update_class(class_id):
    """Update class details. A schema error is returned as a failure, not retried."""
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    cleaned, form_error = _clean_class_form(request.form)
    if form_error:
        return form_error, 400
    payload = {
        "name": cleaned["name"],
        "semester": cleaned["semester"],
        "days": cleaned["days"],
        "section_number": cleaned["section_number"],
        "is_online": cleaned["is_online"],
        "auto_convert_m": cleaned["auto_convert_m"],
    }
    try:
        supabase_admin.table("classes").update(payload).eq("id", class_id).execute()
    except Exception as exc:
        logger.error("Failed to update class %s: %s", class_id, exc)
        return "Failed to update class.", 500
    return redirect(url_for("main.instructor_dashboard"))


@main_bp.route("/class/<class_id>/copy", methods=["POST"])
@login_required
def copy_class(class_id):
    """Duplicate a class for the same instructor: same course settings and LOs; no students or assignments."""
    if session.get("role") != "instructor":
        return redirect(url_for("main.login_page"))

    if not _instructor_owns_class(class_id):
        return redirect(url_for("main.instructor_dashboard"))

    form_token = (request.form.get("copy_class_token") or "").strip()
    session_token = (session.get("copy_class_token") or "").strip()
    if not form_token or form_token != session_token:
        return redirect(url_for("main.instructor_dashboard"))
    if not _consume_one_time_form_token("copy_class", form_token):
        return redirect(url_for("main.instructor_dashboard"))
    session["copy_class_token"] = str(uuid4())

    new_name = (request.form.get("name") or "").strip()
    if not new_name:
        return redirect(url_for("main.instructor_dashboard"))
    new_days, days_error = _clean_class_days(request.form.get("days"))
    if days_error:
        return days_error, 400
    section_number, section_error = _clean_section_number(request.form.get("section_number"))
    if section_error:
        return section_error, 400

    user_id = session["user_id"]

    try:
        src_resp = (
            supabase_admin.table("classes")
            .select("*")
            .eq("id", class_id)
            .eq("instructor_id", user_id)
            .execute()
        )
        if not src_resp.data:
            return redirect(url_for("main.instructor_dashboard"))
        source = src_resp.data[0]

        lo_resp = _select_class_pool_learning_objectives(
            "vendor_code, description, required_ms", class_id
        )
        los = lo_resp.data or []
        lo_specs = []
        for lo in los:
            vc = (lo.get("vendor_code") or "").strip()
            if not vc:
                continue
            spec = {
                "vendor_code": vc,
                "required_ms": int(lo.get("required_ms") or DEFAULT_REQUIRED_MS),
            }
            desc = lo.get("description")
            if desc is not None:
                spec["description"] = desc
            lo_specs.append(spec)

        new_class_data = {
            "name": new_name,
            "semester": source.get("semester") or "",
            "instructor_id": user_id,
        }
        optional_fields = {
            "days": new_days,
            "section_number": section_number,
            "min_masteries": int(source.get("min_masteries") or 2),
            "is_online": bool(source.get("is_online")),
            "auto_convert_m": bool(source.get("auto_convert_m")),
        }

        new_id = None
        full_data = {**new_class_data, **optional_fields}
        ins = supabase_admin.table("classes").insert(full_data).execute()
        if ins.data:
            new_id = ins.data[0].get("id")

        if not new_id:
            return redirect(url_for("main.instructor_dashboard"))

        if lo_specs:
            lo_rows = [{**spec, "class_id": new_id} for spec in lo_specs]
            chunk = 100
            for i in range(0, len(lo_rows), chunk):
                _insert_learning_objectives(lo_rows[i : i + chunk])

        return redirect(url_for("main.instructor_dashboard"))
    except Exception as e:
        logger.error("Error copying class %s: %s", class_id, e)
        return "Failed to copy class.", 500


@main_bp.route("/class/<class_id>/students/<student_id>")
@login_required
def class_student_detail(class_id, student_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))
    class_data = Course.get_full_class_data(class_id)
    if not class_data:
        return redirect(url_for('main.instructor_dashboard'))

    pool = objectives_attached_to_assignments(
        class_id, class_data.get("learning_objectives") or []
    )
    lo_lookup = class_lo_lookup(pool)

    student = None
    for e in class_data.get('enrollments', []):
        if e.get('muted', False):
            continue
        prof = normalize_profile(e)
        if str(prof.get('id') or '') != str(student_id):
            continue
        prof["learning_objectives"], prof["objective_total"] = class_progress(
            prof.get("grades"), lo_lookup
        )
        prof['name'] = display_name(prof.get("full_name") or "")
        prof['email'] = (prof.get('email') or '').strip()
        student = prof
        break

    if student and not (student.get('email') or '').strip():
        try:
            pe = (
                supabase_admin.table("profiles")
                .select("email")
                .eq("id", student_id)
                .limit(1)
                .execute()
            )
            if pe.data:
                student['email'] = (pe.data[0].get('email') or '').strip()
        except Exception:
            pass

    if not student:
        return redirect(url_for('main.class_students', class_id=class_id))

    return render_template(
        "classes/class_student_detail.html",
        class_id=class_id,
        class_name=class_data.get("name"),
        class_days=class_data.get("days"),
        class_section=class_data.get("section_number"),
        student=student,
    )

@main_bp.route("/class/<class_id>/assignments")
@login_required
def class_assignments(class_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))
    try:
        class_data = Course.load_class_record(class_id)
    except Exception as e:
        logger.error("class_assignments: could not load class %s: %s", class_id, e)
        return redirect(url_for('main.instructor_dashboard'))
    if not class_data:
        return redirect(url_for('main.instructor_dashboard'))

    try:
        all_los = Course.get_learning_objectives(class_id)
    except Exception as e:
        logger.error("Error loading LOs for class %s: %s", class_id, e)
        all_los = []

    assignments = load_assignments_for_class(class_id)

    attached_ids = {
        str(lo.get("id") or "")
        for lo in objectives_attached_to_assignments(class_id, all_los or [])
    }
    unattached_count = sum(
        1 for lo in (all_los or [])
        if str(lo.get("id") or "") not in attached_ids
    )

    return render_template(
        "classes/class_assignments.html",
        class_id=class_id,
        class_name=class_data["name"],
        assignments=assignments,
        all_los=all_los,
        unattached_count=unattached_count,
    )


@main_bp.route("/class/<class_id>/create_assignment", methods=["POST"])
@api_instructor_required
def create_assignment(class_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403

    data = request.get_json() or {}

    # Validate required fields
    name = (data.get('name') or '').strip()
    homework_group = Homework.canonicalize_homework_group_for_class(class_id, data.get('homework_group'))

    if not name:
        return jsonify({"success": False, "error": "Assignment name is required."}), 400
    if len(name) > 255:
        return jsonify({"success": False, "error": "Assignment name must be 255 characters or fewer."}), 400
    if not homework_group:
        return jsonify({"success": False, "error": "Homework group is required."}), 400
    if len(homework_group) > 100:
        return jsonify({"success": False, "error": "Homework group must be 100 characters or fewer."}), 400

    date_returned, revision_due, error = clean_assignment_dates(data)
    if error:
        return jsonify({"success": False, "error": error}), 400

    assignment_type = _assignment_type_fields_from_payload(data)

    try:
        # Create new assignment
        result = supabase_admin.table("assignments").insert({
            "class_id": class_id,
            "name": name,
            "homework_group": homework_group,
            "date_returned": date_returned,
            "revision_due": revision_due,
            "assignment_type": assignment_type,
        }).execute()
        
        # Link selected LOs to this assignment (batch insert)
        if result.data:
            assignment_id = result.data[0]['id']
            class_lo_ids = set(Course.get_lo_ids_for_class(class_id) or [])
            ao_rows = []
            for lo_id in data.get('selected_los', []):
                if not lo_id or lo_id not in class_lo_ids:
                    continue
                ao_rows.append({
                    "assignment_id": assignment_id,
                    "learning_objective_id": lo_id,
                })
            if ao_rows:
                _insert_assignment_objectives(ao_rows)
            return jsonify({"success": True, "id": assignment_id})
        return jsonify({"success": False, "error": "Could not create assignment"}), 500
    except Exception as e:
        logger.error("create_assignment failed: %s", e)
        return _safe_api_error("Could not create assignment", 500, log_detail=e)

@main_bp.route("/class/<class_id>/assignments/<assignment_id>/update", methods=["POST", "PUT"])
@api_instructor_required
def update_assignment(class_id, assignment_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    if not _assignment_belongs_to_class(class_id, assignment_id):
        return jsonify({"success": False, "error": "Invalid assignment for class"}), 400

    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    homework_group = Homework.canonicalize_homework_group_for_class(class_id, data.get('homework_group'))
    if not name:
        return jsonify({"success": False, "error": "Assignment name is required."}), 400
    if len(name) > 255:
        return jsonify({"success": False, "error": "Assignment name must be 255 characters or fewer."}), 400
    if not homework_group:
        return jsonify({"success": False, "error": "Homework group is required."}), 400
    if len(homework_group) > 100:
        return jsonify({"success": False, "error": "Homework group must be 100 characters or fewer."}), 400

    date_returned, revision_due, error = clean_assignment_dates(data)
    if error:
        return jsonify({"success": False, "error": error}), 400

    assignment_type = _assignment_type_fields_from_payload(data)

    update_fields = {
        "name": name,
        "homework_group": homework_group,
        "date_returned": date_returned,
        "revision_due": revision_due,
        "assignment_type": assignment_type,
    }

    class_lo_ids = set(Course.get_lo_ids_for_class(class_id) or [])
    selected_ids = []
    seen_selected = set()
    for lo_id in data.get('selected_los', []):
        if not lo_id or lo_id not in class_lo_ids:
            continue
        lo_key = str(lo_id)
        if lo_key in seen_selected:
            continue
        seen_selected.add(lo_key)
        selected_ids.append(lo_key)

    try:
        supabase_admin.table("assignments").update(update_fields).eq(
            "id", assignment_id
        ).eq("class_id", class_id).execute()
    except Exception as e:
        return _safe_api_error("Could not update assignment", 500, log_detail=e)

    try:
        old_rows = fetch_all_rows(
            supabase_admin.table("assignment_objectives")
            .select("learning_objective_id")
            .eq("assignment_id", assignment_id)
            .order("learning_objective_id")
        )
    except Exception as e:
        return _safe_api_error("Could not update assignment", 500, log_detail=e)

    current_ids = []
    seen_current = set()
    for row in old_rows:
        lo_key = str(row.get("learning_objective_id") or "").strip()
        if lo_key and lo_key not in seen_current:
            seen_current.add(lo_key)
            current_ids.append(lo_key)
    current_set = set(current_ids)
    selected_set = set(selected_ids)
    removed = [lo_id for lo_id in current_ids if lo_id not in selected_set]
    added = [lo_id for lo_id in selected_ids if lo_id not in current_set]

    try:
        if added:
            _insert_assignment_objectives([
                {"assignment_id": assignment_id, "learning_objective_id": lo_id}
                for lo_id in added
            ])
    except Exception as e:
        return _safe_api_error("Could not update assignment", 500, log_detail=e)

    try:
        if removed:
            supabase_admin.table("assignment_objectives").delete().eq(
                "assignment_id", assignment_id
            ).in_("learning_objective_id", removed).execute()
    except Exception as e:
        return _safe_api_error("Could not update assignment", 500, log_detail=e)

    try:
        delete_grades_outside_linked_objectives(
            class_id, assignment_id, selected_ids, session.get("user_id")
        )
    except Exception as e:
        return _safe_api_error(
            "Could not delete grades for removed objectives", 500, log_detail=e
        )

    return jsonify({"success": True})


@main_bp.route(
    "/api/class/<class_id>/assignments/<assignment_id>/detach-grade-count",
    methods=["GET"],
)
@api_instructor_required
def api_detach_grade_count(class_id, assignment_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    if not _assignment_belongs_to_class(class_id, assignment_id):
        return jsonify({"success": False, "error": "Invalid assignment for class"}), 400
    selected_ids = []
    seen = set()
    for raw in request.args.getlist("selected_lo"):
        lo_id = str(raw or "").strip()
        if lo_id and lo_id not in seen:
            seen.add(lo_id)
            selected_ids.append(lo_id)
    try:
        rows = grades_outside_linked_objectives(assignment_id, selected_ids)
    except Exception as e:
        return _safe_api_error("Could not count grades", 500, log_detail=e)
    return jsonify({"success": True, "grade_rows": len(rows)})

@main_bp.route("/class/<class_id>/delete_assignment/<assignment_id>", methods=["POST"])
@api_instructor_required
def delete_assignment(class_id, assignment_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403

    try:
        # Verify the assignment actually belongs to this class before touching anything.
        owner_check = (
            supabase_admin.table("assignments")
            .select("id")
            .eq("id", assignment_id)
            .eq("class_id", class_id)
            .limit(1)
            .execute()
        )
        if not owner_check.data:
            return jsonify({"success": False, "error": "Not found"}), 404

        delete_grades_outside_linked_objectives(
            class_id, assignment_id, [], session.get("user_id")
        )

        supabase_admin.table("assignment_objectives").delete().eq(
            "assignment_id", assignment_id
        ).execute()
        supabase_admin.table("assignments").delete().eq(
            "id", assignment_id
        ).eq("class_id", class_id).execute()
        return jsonify({"success": True})
    except Exception as e:
        return _safe_api_error("Could not delete assignment", 500, log_detail=e)


@main_bp.route("/class/<class_id>/assignments/<assignment_id>/export-blank-csv")
@login_required
def export_blank_assignment_csv(class_id, assignment_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))
    if not _assignment_belongs_to_class(class_id, assignment_id):
        return redirect(url_for('main.class_assignments', class_id=class_id))

    bundle = _gradesheet_export_bundle(class_id, assignment_id)
    if not bundle:
        return redirect(url_for('main.class_assignments', class_id=class_id))
    assignment, students, learning_objectives, hw_map = bundle

    assignment_name = assignment.get('name') or 'Assignment'
    date_value = assignment.get('date_returned') or date.today().isoformat()
    vendor_codes = [lo.get('vendor_code') or '' for lo in learning_objectives]
    data_rows = _gradesheet_csv_data_rows(
        students, learning_objectives, hw_map, {}
    )
    csv_text = _build_gradesheet_csv_text(
        assignment_name, date_value, vendor_codes, data_rows, True
    )
    safe_stem = secure_filename(str(assignment_name)) or "assignment"
    filename = f"{safe_stem}_blank_gradesheet.csv"
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@main_bp.route("/class/<class_id>/assignments/<assignment_id>/export-csv")
@login_required
def export_assignment_csv(class_id, assignment_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))
    if not _assignment_belongs_to_class(class_id, assignment_id):
        return redirect(url_for('main.class_reports', class_id=class_id))

    bundle = _gradesheet_export_bundle(class_id, assignment_id)
    if not bundle:
        return redirect(url_for('main.class_reports', class_id=class_id))
    assignment, students, learning_objectives, hw_map = bundle

    assignment_name = assignment.get('name') or 'Assignment'
    date_value = assignment.get('date_returned') or date.today().isoformat()
    vendor_codes = [lo.get('vendor_code') or '' for lo in learning_objectives]
    letter_map = _gradesheet_letter_map_for_assignment(
        assignment_id, students, learning_objectives
    )
    prev_map = Homework.get_hw_prev_scores_map_for_assignment(class_id, assignment_id)
    data_rows = _gradesheet_csv_data_rows(
        students, learning_objectives, hw_map, letter_map, True, prev_map
    )
    csv_text = _build_gradesheet_csv_text(
        assignment_name, date_value, vendor_codes, data_rows, False, True
    )
    safe_stem = secure_filename(str(assignment_name)) or "assignment"
    filename = f"{safe_stem}_gradesheet.csv"
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@main_bp.route("/class/<class_id>/export-archive")
@login_required
@rate_limited("archive_export", limit=5, window_sec=3600)
def export_class_archive(class_id):
    """Download a class archive zip. Does not delete the class or write a row."""
    if not _instructor_owns_class(class_id):
        return redirect(url_for("main.instructor_dashboard"))
    from app.helpers.archive_export import (
        ArchiveClassNotFound,
        ArchiveManifestError,
        build_class_archive_zip,
    )
    try:
        zip_bytes, manifest = build_class_archive_zip(class_id)
    except ArchiveClassNotFound:
        return redirect(url_for("main.instructor_dashboard"))
    except ArchiveManifestError as exc:
        return _safe_api_error("Could not export archive", 500, log_detail=exc)
    too_big = _zip_over_archive_limit(zip_bytes)
    if too_big is not None:
        return too_big
    filename = f"{secure_filename(str(manifest.get('class_name') or '')) or 'class'}_archive.zip"
    return Response(
        zip_bytes,
        mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@main_bp.route("/class/<class_id>/delete_lo/<lo_id>", methods=["POST"])
@api_instructor_required
def delete_lo(class_id, lo_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    try:
        chk = (
            supabase_admin.table("learning_objectives")
            .select("id")
            .eq("id", lo_id)
            .eq("class_id", class_id)
            .limit(1)
            .execute()
        )
        if not chk.data:
            return jsonify({"success": False, "error": "Learning objective not found"}), 404
        # First delete assignment_objectives links
        supabase_admin.table("assignment_objectives").delete().eq("learning_objective_id", lo_id).execute()
        # Then delete grades
        try:
            existing = supabase_admin.table("grades").select(
                "student_id, learning_objective_id, assignment_id, top_score, second_score, counts_for_mastery"
            ).eq("learning_objective_id", lo_id).execute()
            _log_grade_deletions(existing.data or [], class_id, session['user_id'])
        except Exception as e:
            logger.warning("Could not audit-log grade deletions for lo %s: %s", lo_id, e)
        supabase_admin.table("grades").delete().eq("learning_objective_id", lo_id).execute()
        # Then delete the LO
        supabase_admin.table("learning_objectives").delete().eq("id", lo_id).eq("class_id", class_id).execute()
        return jsonify({"success": True})
    except Exception as e:
        logger.exception("delete_lo failed class_id=%s lo_id=%s", class_id, lo_id)
        return _safe_api_error("Could not delete learning objective", 500, log_detail=e)


@main_bp.route("/api/class/<class_id>/update-lo/<lo_id>", methods=["POST"])
@api_instructor_required
def api_update_learning_objective(class_id, lo_id):
    """Update display fields on an existing LO (same row id — grades and links stay intact)."""
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    data, err = _require_json_object()
    if err:
        return err[0], err[1]
    try:
        cur = (
            supabase_admin.table("learning_objectives")
            .select("id, vendor_code, description, required_ms")
            .eq("id", lo_id)
            .eq("class_id", class_id)
            .limit(1)
            .execute()
        )
        if not cur.data:
            return jsonify({"success": False, "error": "Learning objective not found"}), 404
        row = cur.data[0]
        vendor_raw = data.get("vendor_code")
        if vendor_raw is None:
            vendor_code = (row.get("vendor_code") or "").strip() or None
        else:
            vendor_code = str(vendor_raw).strip() or None
        if not vendor_code:
            return jsonify({"success": False, "error": "Objective code is required"}), 400
        if len(vendor_code) > 50:
            return jsonify({"success": False, "error": "Objective code must be 50 characters or fewer."}), 400
        if vendor_code and _lo_vendor_code_conflict(class_id, vendor_code, lo_id):
            return jsonify(
                {"success": False, "error": "Another objective in this class already uses that code."}
            ), 400
        desc_raw = data.get("description")
        if desc_raw is None:
            description = row.get("description")
        else:
            description = str(desc_raw).strip() or None
        if description and len(description) > 2000:
            return jsonify({"success": False, "error": "Description must be 2000 characters or fewer."}), 400
        if data.get("required_ms") is None:
            req = int(row.get("required_ms") or DEFAULT_REQUIRED_MS)
        else:
            try:
                req = int(data.get("required_ms"))
            except (TypeError, ValueError):
                req = DEFAULT_REQUIRED_MS
        req = max(1, min(5, req))
        supabase_admin.table("learning_objectives").update(
            {
                "vendor_code": vendor_code,
                "description": description,
                "required_ms": req,
            }
        ).eq("id", lo_id).eq("class_id", class_id).execute()
        return jsonify({"success": True})
    except Exception as e:
        logger.exception("api_update_learning_objective failed class_id=%s lo_id=%s", class_id, lo_id)
        return _safe_api_error("Could not update learning objective", 500, log_detail=e)


@main_bp.route("/api/class/<class_id>/preview-learning-objectives", methods=["POST"])
@api_instructor_required
def api_preview_learning_objectives(class_id):
    """Parse LO CSV; return rows for confirm UI (Ms edited client-side)."""
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403

    err, rows, parse_warnings = _parse_lo_csv_upload()
    if err:
        return jsonify({"success": False, "error": err, "warnings": parse_warnings}), 400

    preview_rows = annotate_learning_objective_rows_for_preview(class_id, rows or [])
    return jsonify(
        {
            "success": True,
            "rows": preview_rows,
            "warnings": parse_warnings,
            "count": len(preview_rows),
        }
    )


@main_bp.route("/api/class/<class_id>/import-learning-objectives", methods=["POST"])
@api_instructor_required
def api_import_learning_objectives(class_id):
    """Commit LO rows after user confirms (JSON body: { \"rows\": [...] })."""
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403

    data, err = _require_json_object()
    if err:
        return err[0], err[1]
    raw_rows = data.get("rows")
    if not isinstance(raw_rows, list) or len(raw_rows) == 0:
        return jsonify({"success": False, "error": "Missing or empty rows array"}), 400
    if len(raw_rows) > MAX_IMPORT_ROWS:
        return jsonify({"success": False, "error": f"Too many rows (max {MAX_IMPORT_ROWS})"}), 400

    rows: List[Dict[str, Any]] = []
    for item in raw_rows:
        if not isinstance(item, dict):
            continue
        vc = (item.get("vendor_code") or "").strip()
        if not vc:
            continue
        try:
            req = int(item.get("required_ms", DEFAULT_REQUIRED_MS))
        except (TypeError, ValueError):
            req = DEFAULT_REQUIRED_MS
        req = max(1, min(5, req))
        desc_raw = item.get("description")
        description = str(desc_raw).strip() if desc_raw is not None else ""
        rows.append(
            {
                "vendor_code": vc,
                "required_ms": req,
                "description": description or None,
            }
        )

    if not rows:
        return jsonify({"success": False, "error": "No valid rows to import"}), 400

    inserted, skipped, errors = import_learning_objectives_rows(class_id, rows)
    if errors and inserted == 0:
        return jsonify(
            {
                "success": False,
                "error": errors[0],
                "inserted": 0,
                "skipped": skipped,
            }
        ), 500

    return jsonify(
        {
            "success": True,
            "inserted": inserted,
            "skipped": skipped,
            "errors": errors,
        }
    )


@main_bp.route("/class/<class_id>/reports")
@login_required
def class_reports(class_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))
    class_data = Course.get_full_class_data(class_id)
    
    if not class_data:
        return redirect(url_for('main.instructor_dashboard'))

    progress_data = _class_data_for_progress(class_data)
    students, _, _ = _process_enrollments(progress_data)
    learning_objectives = progress_data.get("learning_objectives") or []

    # Load assignments with their linked LOs and dates
    assignments = load_assignments_for_class(class_id, desc=False)

    return render_template(
        "classes/class_reports.html",
        class_id=class_id,
        class_name=class_data["name"],
        class_days=class_data.get("days"),
        class_section=class_data.get("section_number"),
        students=students,
        learning_objectives=learning_objectives,
        assignments=assignments,
    )


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


def _revision_class_title(class_id):
    resp = (
        supabase_admin.table("classes")
        .select("name, days, section_number")
        .eq("id", class_id)
        .limit(1)
        .execute()
    )
    row = {}
    if isinstance(resp.data, list) and resp.data and isinstance(resp.data[0], dict):
        row = resp.data[0]
    return class_display_title(
        row.get("name"),
        row.get("days"),
        row.get("section_number"),
    )


def _public_reminder_student(student):
    item = {
        "student_id": student["student_id"],
        "name": student["name"],
    }
    if student.get("email"):
        item["email"] = student["email"]
    if student.get("message"):
        item["message"] = student["message"]
    return item


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


@main_bp.route("/class/<class_id>/student/<student_id>/history")
@login_required
def student_history(class_id, student_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))
    class_data = Course.get_full_class_data(class_id)
    if not class_data:
        return redirect(url_for('main.instructor_dashboard'))

    # Get student enrollment and profile info
    enrollment_data = None
    try:
        enrollment = supabase_admin.table("enrollments").select(
            "student_id, profiles(id, full_name, email)"
        ).eq("class_id", class_id).eq("student_id", student_id).single().execute()
        enrollment_data = enrollment.data
    except Exception as e:
        logger.error("Error loading student enrollment: %s", e)
        return redirect(url_for('main.class_reports', class_id=class_id))

    if not enrollment_data:
        return redirect(url_for('main.class_reports', class_id=class_id))

    profile = normalize_profile({'profiles': enrollment_data.get('profiles')})
    student_name = display_name(profile.get("full_name") or "")
    student_email = (profile.get("email") or "").strip()
    profile_id = profile.get('id')

    assignments = load_assignments_for_class(class_id)
    learning_objectives = objectives_attached_to_assignments(
        class_id, class_data.get("learning_objectives") or []
    )
    lo_ids = _history_objective_ids(learning_objectives)
    all_grades: List[Dict[str, Any]] = []
    try:
        for start in range(0, len(lo_ids), _HISTORY_LO_CHUNK):
            chunk = lo_ids[start:start + _HISTORY_LO_CHUNK]
            all_grades.extend(fetch_all_rows(
                supabase_admin.table("grades")
                .select(_HISTORY_GRADE_COLS)
                .eq("student_id", profile_id)
                .in_("learning_objective_id", chunk)
                .order("id")
            ))
    except Exception as e:
        logger.error("Error loading grades: %s", e)
        all_grades = []
    all_grades = _grades_for_this_class(all_grades, assignments)
    lo_grade_data, history_assignments = student_history_sheet(
        learning_objectives, assignments, all_grades
    )

    return render_template(
        "classes/student_history.html",
        class_id=class_id,
        class_name=class_data['name'],
        class_days=class_data.get("days"),
        class_section=class_data.get("section_number"),
        student_id=student_id,
        student_name=student_name,
        student_email=student_email,
        instructor_title=instructor_title(_report_sender_name()),
        learning_objectives=lo_grade_data,
        history_assignments=history_assignments,
    )

@main_bp.route("/class/<class_id>/student-history/print")
@login_required
def student_history_print(class_id):
    if not _instructor_owns_class(class_id):
        return "Forbidden", 403
    try:
        class_resp = (
            supabase_admin.table("classes")
            .select("id, name, days, section_number")
            .eq("id", class_id)
            .limit(1)
            .execute()
        )
        class_row = (class_resp.data or [None])[0]
        if not class_row:
            return redirect(url_for("main.instructor_dashboard"))
        objectives = Course.get_learning_objectives(class_id) or []
        enrollments = fetch_all_rows(
            supabase_admin.table("enrollments")
            .select(
                "id, class_id, student_id, muted, "
                "profiles(id, full_name, role, email)"
            )
            .eq("class_id", class_id)
            .order("id")
        )
        class_data = {
            "id": class_row.get("id") or class_id,
            "name": class_row.get("name"),
            "days": class_row.get("days"),
            "section_number": class_row.get("section_number"),
            "learning_objectives": objectives,
            "enrollments": enrollments,
        }
        progress = _class_data_for_progress(class_data)
        students, _, _ = _process_enrollments(progress or class_data)
        learning_objectives = (progress or class_data).get("learning_objectives") or []
        assignments = load_assignments_for_class(class_id)
        packet = _grades_for_this_class(
            _history_packet_grades(
                [lo.get("id") for lo in learning_objectives if lo.get("id")]
            ),
            assignments,
        )
    except Exception as e:
        logger.error("student history print load failed: %s", type(e).__name__)
        return _HISTORY_PRINT_ERROR, 500

    active_ids = {
        str(student.get("id")) for student in students if student.get("id")
    }
    grades_by_student: Dict[str, List[Dict[str, Any]]] = {}
    for grade in packet:
        student_id = str(grade.get("student_id") or "")
        if student_id not in active_ids:
            continue
        grades_by_student.setdefault(student_id, []).append(grade)

    sheets = []
    for student in students:
        student_id = str(student.get("id") or "")
        lo_grade_data, history_assignments = student_history_sheet(
            learning_objectives,
            assignments,
            grades_by_student.get(student_id, []),
        )
        sheets.append({
            "student_name": student.get("name") or "",
            "learning_objectives": lo_grade_data,
            "history_assignments": history_assignments,
        })

    return render_template(
        "classes/student_history_print.html",
        class_id=class_id,
        class_name=class_data["name"],
        sheets=sheets,
    )


@main_bp.route("/class/<class_id>/speed_grader", endpoint='class_speed_grader')
@login_required
def class_speed_grader(class_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))

    try:
        class_data = Course.load_class_record(class_id)
    except Exception as e:
        logger.error("class_speed_grader: could not load class %s: %s", class_id, e)
        return redirect(url_for('main.instructor_dashboard'))
    if not class_data:
        return redirect(url_for('main.instructor_dashboard'))

    hw_passes_allowed = FREE_PASSES_ALLOWED

    try:
        raw_enrollments = Course.load_enrollments(class_id)
    except Exception as e:
        logger.error("class_speed_grader: could not load enrollments for %s: %s", class_id, e)
        return redirect(url_for('main.instructor_dashboard'))

    assignments = load_assignments_for_class(class_id)

    students = []
    for enrollment in raw_enrollments:
        prof = normalize_profile(enrollment)
        if not prof.get('id'):
            continue
        prof['name'] = display_name(prof.get("full_name") or "")
        if enrollment.get('muted', False):
            continue
        students.append(prof)

    if students:
        students.sort(key=row_sort_key)
        student_ids = [s['id'] for s in students if s.get('id')]
        passes_map = _batch_get_free_passes(student_ids, class_id)
        for prof in students:
            prof['passes_remaining'] = max(0, hw_passes_allowed - passes_map.get(prof['id'], 0))

    lo_names = []
    try:
        lo_names = Course.get_learning_objectives(class_id)
    except Exception as e:
        logger.error("Error loading LOs: %s", e)

    return render_template("classes/class_speed_grader.html",
                           class_id=class_id,
                           class_name=class_data.get('name'),
                           assignments=assignments,
                           students=students,
                           lo_names=lo_names,
                           auto_convert_m=class_data.get('auto_convert_m', False))

@main_bp.route("/class/<class_id>/update_grade", methods=["GET"], endpoint='upload_grades')
@login_required
def update_grade_handler(class_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))

    try:
        class_data = Course.load_class_record(class_id)
    except Exception as e:
        logger.error("update_grade_handler: could not load class %s: %s", class_id, e)
        return redirect(url_for('main.instructor_dashboard'))
    if not class_data:
        return redirect(url_for('main.instructor_dashboard'))

    try:
        enrollments = Course.load_enrollments(class_id)
    except Exception as e:
        logger.error("update_grade_handler: could not load enrollments for %s: %s", class_id, e)
        return redirect(url_for('main.instructor_dashboard'))

    assignments = load_assignments_for_class(class_id)
    template_kwargs = {
        "class_id": class_id,
        "class_name": class_data.get("name"),
        "assignments": assignments,
    }

    roster = []
    for enrollment in enrollments or []:
        prof = normalize_profile(enrollment)
        sid = str(prof.get("id") or "").strip()
        if not sid:
            continue
        roster.append({
            "id": sid,
            "full_name": prof.get("full_name") or "",
        })
    roster.sort(key=row_sort_key)
    template_kwargs["roster"] = roster

    return render_template("classes/update_grade.html", **template_kwargs)


@main_bp.route("/class/<class_id>/create_learning_objective", methods=["GET", "POST"], endpoint='create_learning_objective')
@login_required
def create_lo_handler(class_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))

    class_data = Course.get_full_class_data(class_id)
    if not class_data:
        return redirect(url_for('main.instructor_dashboard'))

    if request.method == "POST":
        # Create a new learning objective. (Assignment creation/editing is
        # handled separately by create_assignment / update_assignment, which
        # the current UI actually calls.)
        lo_code = request.form.get('code', '').strip()
        lo_description = request.form.get('description', '').strip()
        lo_required_ms = request.form.get('required_ms', 2)

        if lo_code and len(lo_code) > 50:
            return "Objective code must be 50 characters or fewer.", 400
        if lo_description and len(lo_description) > 2000:
            return "Description must be 2000 characters or fewer.", 400
        if lo_code:
            try:
                req_ms = int(lo_required_ms)
            except (TypeError, ValueError):
                req_ms = DEFAULT_REQUIRED_MS
            req_ms = max(1, min(5, req_ms))
            try:
                _insert_learning_objectives({
                    "class_id": class_id,
                    "vendor_code": lo_code,
                    "description": lo_description or None,
                    "required_ms": req_ms
                })
            except Exception as e:
                logger.error("Error creating LO: %s", e)

        if request.form.get("return_to", "").strip() == "dashboard":
            return redirect(url_for("main.class_detail", class_id=class_id))
        return redirect(url_for('main.class_assignments', class_id=class_id))

    # GET requests just redirect back to the objectives page (modal handles creation)
    return redirect(url_for('main.class_assignments', class_id=class_id))

# ============================================================================
# API & ACTION ROUTES
# ============================================================================

@main_bp.route("/add_class", methods=["POST"])
@login_required
def add_class():
    # Defense-in-depth: the create_class_token below is only ever issued by
    # instructor_dashboard (which itself checks role == 'instructor'), so a
    # non-instructor session shouldn't be able to reach this point at all
    # today. Checking the role directly here too means this can't silently
    # regress if the token-issuing flow is ever refactored.
    if (session.get('role') or '').strip().lower() != 'instructor':
        return redirect(url_for('main.login_page'))

    cleaned, form_error = _clean_class_form(request.form)
    if form_error:
        return form_error, 400

    form_token = (request.form.get("create_class_token") or "").strip()
    session_token = (session.get("create_class_token") or "").strip()
    if not form_token or form_token != session_token:
        return redirect(url_for('main.instructor_dashboard'))
    if not _consume_one_time_form_token("create_class", form_token):
        return redirect(url_for('main.instructor_dashboard'))
    # Rotate token immediately so refresh/back cannot replay this post.
    session['create_class_token'] = str(uuid4())

    user_id = session['user_id']

    try:
        new_class_data = {
            "name": cleaned["name"],
            "semester": cleaned["semester"],
            "instructor_id": user_id,
        }
        # Optional columns — only include if the form provides them.
        # Each requires a matching column in the Supabase classes table.
        optional_fields = {
            "days": cleaned["days"],
            "section_number": cleaned["section_number"],
            "min_masteries": int(request.form.get("min_masteries") or 2),
            "is_online": cleaned["is_online"],
        }
        if cleaned["auto_convert_m"]:
            optional_fields["auto_convert_m"] = True

        full_data = {**new_class_data, **optional_fields}
        supabase_admin.table("classes").insert(full_data).execute()
        return redirect(url_for('main.instructor_dashboard'))
    except Exception as e:
        logger.error("Failed to create class: %s", e)
        return "Failed to create class.", 500


@main_bp.route("/api/class/<class_id>/save-grades", methods=["POST"])
@api_login_required
def save_grades(class_id):
    try:
        if not _instructor_owns_class(class_id):
            return jsonify({"success": False, "error": "Forbidden"}), 403
        data = request.get_json()
        grades_dict = data.get('grades', {})
        assignment_id = data.get('assignment_id')
        if assignment_id and not _assignment_belongs_to_class(class_id, str(assignment_id)):
            return jsonify({"success": False, "error": "Invalid assignment for class"}), 400
        allowed_lo_ids = _allowed_lo_ids_for_grading(
            class_id, str(assignment_id) if assignment_id else None
        )
        enrolled_student_ids = _enrolled_student_ids_for_class(class_id)
        auto_convert = _class_auto_convert_m_enabled(class_id) if assignment_id else False
        hw_map = Homework.get_hw_scores_map_for_assignment(class_id, assignment_id) if assignment_id else {}

        # Pre-collect the (student, lo) pairs we're about to write so we can
        # batch-fetch the existing rows in ONE query and implement first-entry-
        # only stickiness for the counts_for_mastery flag. Cells with an empty/
        # cleared value are routed to `to_clear` instead of being silently
        # dropped -- an explicit clear should delete any existing grade row.
        incoming = []
        to_clear = []
        for key, grade_value in grades_dict.items():
            parts = key.split('|')
            if len(parts) != 2:
                continue
            student_id, lo_id = parts
            if not lo_id:
                continue
            if str(lo_id) not in allowed_lo_ids:
                continue
            if str(student_id).strip() not in enrolled_student_ids:
                continue
            if not grade_value:
                to_clear.append({'student_id': student_id, 'lo_id': lo_id})
                continue
            normalized = Grade.normalize_score(grade_value)
            if not normalized:
                continue
            if (
                assignment_id
                and normalized != "A"
                and not Homework.student_has_recorded_score(hw_map, student_id)
            ):
                return jsonify({
                    "success": False,
                    "error": _HW_REQUIRED_FOR_MARK_ERROR,
                }), 400
            incoming.append({
                'student_id': student_id,
                'lo_id': lo_id,
                'normalized': normalized,
                'sid_key': str(student_id).strip(),
            })

        # Batch-fetch existing rows so we can preserve the original
        # counts_for_mastery for any row that already holds an M/MR. A row is
        # only "first-entry" if no row exists yet or the existing row holds a
        # non-mastery letter (R, RQ, P, X, A).
        existing_by_key: Dict[str, Dict[str, Any]] = {}
        if incoming and assignment_id:
            sids = list({it['student_id'] for it in incoming})
            lo_ids = list({it['lo_id'] for it in incoming})
            ex_resp = supabase_admin.table("grades").select(
                "student_id, learning_objective_id, top_score, counts_for_mastery"
            ).in_("student_id", sids).in_("learning_objective_id", lo_ids).eq(
                "assignment_id", assignment_id
            ).execute()
            for r in (ex_resp.data or []):
                k = f"{r['student_id']}|{r['learning_objective_id']}"
                existing_by_key[k] = r

        # Build batch of grade rows and upsert in one call.
        grade_rows = []
        for it in incoming:
            student_id = it['student_id']
            lo_id = it['lo_id']
            normalized = it['normalized']
            sid = it['sid_key']
            key = f"{student_id}|{lo_id}"

            # counts_for_mastery semantics: only meaningful when the saved
            # letter is M or MR. Default True for everything else so the column
            # stays simple. Non-counting status is FIRST-ENTRY-ONLY: once a row
            # holds M/MR with a flag, re-saving M/MR keeps the existing flag,
            # even if HW% has changed.
            counts_for_mastery = True
            if normalized in ('M', 'MR'):
                existing = existing_by_key.get(key)
                existing_top = (existing or {}).get('top_score')
                if existing and existing_top in ('M', 'MR'):
                    prev_flag = existing.get('counts_for_mastery')
                    counts_for_mastery = True if prev_flag is None else bool(prev_flag)
                elif auto_convert:
                    # First-entry M/MR: capture HW eligibility. Non-A marks
                    # already required a recorded HW score above, so sid is in hw_map.
                    if sid in hw_map:
                        counts_for_mastery = bool(
                            Homework.is_exam_grade_eligible_hw_score(hw_map[sid])
                        )
                    else:
                        counts_for_mastery = True
                else:
                    counts_for_mastery = True

            row = {
                "student_id": student_id,
                "learning_objective_id": lo_id,
                "top_score": normalized,
                "counts_for_mastery": counts_for_mastery,
                "last_modified_by": session['user_id'],
                "hw_score_at_entry": hw_map.get(sid) if sid in hw_map else None,
            }
            if assignment_id:
                row["assignment_id"] = assignment_id
            grade_rows.append(row)

        if grade_rows:
            supabase_admin.table("grades").upsert(
                grade_rows, on_conflict="student_id,learning_objective_id,assignment_id"
            ).execute()

        if to_clear:
            _clear_grade_cells_for_assignment(
                class_id, assignment_id, to_clear, session.get("user_id")
            )

        logger.info(
            "grade save user_id=%s class_id=%s",
            session.get("user_id"),
            class_id,
        )
        return jsonify({"success": True})
    except Exception as e:
        return _safe_api_error("Could not save grades", 500, log_detail=e)


@main_bp.route("/api/class/<class_id>/assignment/<assignment_id>/grades")
@api_login_required
def api_assignment_grades(class_id, assignment_id):
    """Return grades for a specific assignment, keyed by student_id|lo_id."""
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    try:
        assignment_resp = (
            supabase_admin.table("assignments")
            .select("id, homework_group, assignment_type")
            .eq("id", assignment_id)
            .eq("class_id", class_id)
            .limit(1)
            .execute()
        )
    except Exception:
        return jsonify({"success": False, "error": "Invalid assignment for class"}), 400
    assignment_row = (assignment_resp.data or [None])[0] or {}
    if not assignment_row.get("id"):
        return jsonify({"success": False, "error": "Invalid assignment for class"}), 400
    raw_type = assignment_row.get("assignment_type") or "mastery_opp"
    assignment_type = raw_type if raw_type in ASSIGNMENT_TYPES else "mastery_opp"
    try:
        result = (
            supabase_admin.table("grades")
            .select("student_id, learning_objective_id, top_score, counts_for_mastery")
            .eq("assignment_id", assignment_id)
            .execute()
        )
        grades_map = {}
        counts_for_mastery_map = {}
        has_assignment_grades = False
        for grade in (result.data or []):
            if str(grade.get("top_score") or "").strip():
                has_assignment_grades = True
            key = f"{grade['student_id']}|{grade['learning_objective_id']}"
            grades_map[key] = grade['top_score']
            flag = grade.get('counts_for_mastery')
            counts_for_mastery_map[key] = True if flag is None else bool(flag)

        # Import / legacy rows often have assignment_id NULL while still targeting LOs on this
        # assignment. Class reports load all grade rows per student, so those marks appeared
        # there but not here when we only filtered by assignment_id. Fill gaps from unscoped rows
        # for LOs linked to this assignment (explicit assignment-scoped rows win if both exist).
        # When an assignment has no linked learning objectives, fall back to every
        # learning objective in the class so those grades still show here the same
        # way they do on the Reports tab.
        ao_resp = supabase_admin.table("assignment_objectives") \
            .select("learning_objective_id") \
            .eq("assignment_id", assignment_id) \
            .execute()
        lo_ids = [
            r["learning_objective_id"]
            for r in (ao_resp.data or [])
            if r.get("learning_objective_id")
        ]
        if not lo_ids:
            try:
                lo_ids = Course.get_lo_ids_for_class(class_id) or []
            except Exception as lo_err:
                logger.error(
                    "Error loading class LO ids for unscoped grade fallback (class=%s): %s",
                    class_id,
                    lo_err,
                )
                lo_ids = []
        if lo_ids:
            unscoped = (
                supabase_admin.table("grades")
                .select("student_id, learning_objective_id, top_score, counts_for_mastery")
                .is_("assignment_id", None)
                .in_("learning_objective_id", lo_ids)
                .execute()
            )
            for grade in (unscoped.data or []):
                key = f"{grade['student_id']}|{grade['learning_objective_id']}"
                if key not in grades_map:
                    grades_map[key] = grade["top_score"]
                    flag = grade.get('counts_for_mastery')
                    counts_for_mastery_map[key] = True if flag is None else bool(flag)

        hw_scores, hw_prev_scores = Homework.load_hw_score_and_prev_maps(
            class_id, assignment_id, assignment_row
        )
        eligibility = {}
        for sid, score in hw_scores.items():
            eligibility[sid] = score == -1 or (
                score is not None and score >= Homework.EXAM_GRADE_HW_THRESHOLD
            )

        return jsonify({
            "success": True,
            "grades": grades_map,
            "has_assignment_grades": has_assignment_grades,
            "counts_for_mastery_map": counts_for_mastery_map,
            "hw_scores": hw_scores,
            "hw_prev_scores": hw_prev_scores,
            "revision_eligible": eligibility,
            "assignment_type": assignment_type,
        })
    except Exception as e:
        return _safe_api_error("Could not load assignment grades", 500, log_detail=e)


@main_bp.route("/api/class/<class_id>/save-hw-percentage", methods=["POST"])
@api_login_required
def save_hw_percentage(class_id):
    try:
        if not _instructor_owns_class(class_id):
            return jsonify({"success": False, "error": "Forbidden"}), 403
        data = request.get_json()
        student_id = data.get('student_id')
        score = data.get('score')
        assignment_id = data.get('assignment_id')
        if student_id is None or score is None or not assignment_id:
            return jsonify({"success": False, "error": "student_id, score, and assignment_id required"}), 400
        score = int(score)
        if score != -1:
            score = max(0, min(100, score))
        # Accept IDs with incidental whitespace from client-side state.
        class_id = str(class_id).strip()
        assignment_id = str(assignment_id).strip()
        if not _assignment_belongs_to_class(class_id, assignment_id):
            return jsonify({"success": False, "error": "Assignment not found for this class"}), 404
        if not _student_enrolled_in_class(class_id, str(student_id).strip()):
            return jsonify({"success": False, "error": "Student is not enrolled in this class"}), 403

        hw_key = Homework.resolve_hw_group_storage_key(class_id, assignment_id)
        if hw_key is None:
            client_hg = Homework.normalize_homework_group(data.get("homework_group"))
            hw_key = client_hg or assignment_id
        hw_group = str(hw_key).strip()
        row = {
            "student_id": student_id,
            "class_id": class_id,
            "homework_group": hw_group,
            "score_pct": score,
        }
        # Single-row list so PostgREST gets explicit columns=… (more reliable than a bare dict).
        supabase_admin.table("homework_scores").upsert(
            [row],
            on_conflict="student_id,class_id,homework_group",
        ).execute()

        promoted = 0
        if score == -1:
            promoted = _promote_non_counting_masteries_for_student(class_id, student_id, changed_by=session['user_id'])

        return jsonify({"success": True, "promoted_masteries": promoted})
    except Exception as e:
        logger.error("saving hw percentage failed: %s", e)
        return _safe_api_error("Could not save homework score", 500, log_detail=e)


@main_bp.route("/api/class/<class_id>/use_pass", methods=["POST"])
@api_login_required
def use_free_pass(class_id):
    try:
        if not _instructor_owns_class(class_id):
            return jsonify({"success": False, "error": "Forbidden"}), 403
        data = request.get_json()
        student_id = data.get('student_id')
        if not student_id:
            return jsonify({"success": False, "error": "student_id required"}), 400
        if not _student_enrolled_in_class(class_id, str(student_id)):
            return jsonify({"success": False, "error": "Student is not enrolled in this class"}), 403

        passes_allowed = FREE_PASSES_ALLOWED

        existing = supabase_admin.table("free_passes") \
            .select("id, passes_used") \
            .eq("student_id", student_id) \
            .eq("class_id", class_id) \
            .execute()

        if existing.data:
            current_used = existing.data[0]['passes_used']
            if current_used >= passes_allowed:
                return jsonify({"success": False, "error": "No passes remaining"}), 400
            supabase_admin.table("free_passes") \
                .update({"passes_used": current_used + 1}) \
                .eq("id", existing.data[0]['id']) \
                .execute()
            remaining = passes_allowed - (current_used + 1)
        else:
            supabase_admin.table("free_passes").insert({
                "student_id": student_id,
                "class_id": class_id,
                "passes_used": 1
            }).execute()
            remaining = passes_allowed - 1

        return jsonify({"success": True, "passes_remaining": remaining})
    except Exception as e:
        logger.error("use pass failed: %s", e)
        return _safe_api_error("Could not use free pass", 500, log_detail=e)


@main_bp.route("/api/class/<class_id>/return_pass", methods=["POST"])
@api_login_required
def return_free_pass(class_id):
    try:
        if not _instructor_owns_class(class_id):
            return jsonify({"success": False, "error": "Forbidden"}), 403
        data = request.get_json()
        student_id = data.get('student_id')
        if not student_id:
            return jsonify({"success": False, "error": "student_id required"}), 400
        if not _student_enrolled_in_class(class_id, str(student_id)):
            return jsonify({"success": False, "error": "Student is not enrolled in this class"}), 403

        passes_allowed = FREE_PASSES_ALLOWED

        existing = supabase_admin.table("free_passes") \
            .select("id, passes_used") \
            .eq("student_id", student_id) \
            .eq("class_id", class_id) \
            .execute()

        if existing.data and existing.data[0]['passes_used'] > 0:
            current_used = existing.data[0]['passes_used']
            supabase_admin.table("free_passes") \
                .update({"passes_used": current_used - 1}) \
                .eq("id", existing.data[0]['id']) \
                .execute()
            remaining = passes_allowed - (current_used - 1)
        else:
            remaining = passes_allowed

        return jsonify({"success": True, "passes_remaining": remaining})
    except Exception as e:
        logger.error("return pass failed: %s", e)
        return _safe_api_error("Could not return free pass", 500, log_detail=e)


@main_bp.route("/api/class/<class_id>/upload_students", methods=["POST"])
@api_instructor_required
@rate_limited("upload_students", limit=20, window_sec=900)
def api_upload_students_to_class(class_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    err, rows, parse_errors = _parse_student_csv_upload()
    if err:
        return jsonify({
            "success": False,
            "error": err,
            "errors": parse_errors,
        }), 400

    try:
        errors, actions = _classify_student_csv_rows(
            class_id, session.get("user_id"), rows or [], parse_errors
        )
        if errors:
            return _student_csv_rejection(errors)
        created = 0
        enrolled = 0
        for action in actions:
            if action["outcome"] == "reuse":
                supabase_admin.table("enrollments").insert({
                    "class_id": class_id,
                    "student_id": action["profile_id"],
                }).execute()
                enrolled += 1
                continue
            student_id = str(uuid4())
            supabase_admin.table("profiles").insert({
                "id": student_id,
                "full_name": action["full_name"],
                "role": "student",
                "email": action["email"],
                "email_set_at": _email_set_timestamp(),
            }).execute()
            supabase_admin.table("enrollments").insert({
                "class_id": class_id,
                "student_id": student_id,
            }).execute()
            created += 1
        return jsonify({
            "success": True,
            "message": "Student upload complete",
            "stats": {
                "created_profiles": created,
                "enrolled_existing_profiles": enrolled,
            },
            "total_rows": len(actions),
        })
    except Exception as e:
        logger.error("Error uploading students for class %s: %s", class_id, e)
        return _safe_api_error("Could not upload students", 500, log_detail=e)


@main_bp.route("/api/class/<class_id>/preview-upload-students", methods=["POST"])
@api_instructor_required
@rate_limited("preview_upload_students", limit=30, window_sec=900)
def api_preview_upload_students(class_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    err, rows, parse_errors = _parse_student_csv_upload()
    if err:
        return jsonify({
            "success": False,
            "error": err,
            "errors": parse_errors,
        }), 400

    try:
        errors, actions = _classify_student_csv_rows(
            class_id, session.get("user_id"), rows or [], parse_errors
        )
        if errors:
            return _student_csv_rejection(errors)
        preview_rows = []
        will_create = 0
        will_enroll = 0
        for action in actions:
            if action["outcome"] == "reuse":
                status = "Will enroll"
                will_enroll += 1
            else:
                status = "Will create and enroll"
                will_create += 1
            preview_rows.append({
                "full_name": action["full_name"],
                "email": action["email"],
                "action": action["outcome"],
                "status": status,
            })
        return jsonify({
            "success": True,
            "rows": preview_rows,
            "stats": {
                "will_create": will_create,
                "will_enroll_existing": will_enroll,
            },
            "count": len(preview_rows),
        })
    except Exception as e:
        logger.error("Error previewing student upload for class %s: %s", class_id, e)
        return _safe_api_error("Could not preview student upload", 500, log_detail=e)


@main_bp.route("/api/class/<class_id>/toggle_mute", methods=["POST"])
@api_instructor_required
def api_toggle_mute(class_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    try:
        data = request.get_json()
        student_id = data.get('student_id')
        muted = bool(data.get('muted', False))
        if not student_id:
            return jsonify({"success": False, "error": "student_id is required"}), 400
        if not _student_enrolled_in_class(class_id, student_id):
            return jsonify({"success": False, "error": "Student not enrolled in this class"}), 403
        supabase_admin.table("enrollments").update({"muted": muted}) \
            .eq("class_id", class_id).eq("student_id", student_id).execute()
        return jsonify({"success": True, "muted": muted}), 200
    except Exception as e:
        return _safe_api_error("Could not update mute state", 500, log_detail=e)


@main_bp.route("/api/import-grades", methods=["POST"])
@api_login_required
@rate_limited("import_grades", limit=30, window_sec=900)
def api_import_grades():

    data = request.get_json() or {}
    class_id = data.get('class_id')
    assignment_id = data.get('assignment_id')
    students = data.get('students', []) or []
    extracted_los = data.get('learning_objectives', []) or []
    if not class_id:
        return jsonify({"success": False, "error": "Missing class_id"}), 400
    if not _instructor_owns_class(str(class_id)):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    if assignment_id and not _assignment_belongs_to_class(str(class_id), str(assignment_id)):
        return jsonify({"success": False, "error": "Invalid assignment for class"}), 400
    if len(students) > MAX_IMPORT_ROWS:
        return jsonify({"success": False, "error": f"Too many students in one import (max {MAX_IMPORT_ROWS})"}), 400

    # Keep import-only HW % columns out of mastery LO creation/linking.
    extracted_los = [
        lo for lo in extracted_los
        if not Homework.is_import_sheet_hw_column(lo)
        and not Homework.is_hw_prev_header(lo)
    ]

    student_entries: List[Tuple[str, Dict[str, Any]]] = []
    for s in students:
        nm = (s.get('name') or s.get('full_name') or '').strip()
        if not nm:
            continue
        student_entries.append((nm, s))

    # One entry per name_key, using the first sheet spelling. Grade rows for
    # that key still merge before the batch upsert.
    seen_name_keys: set = set()
    unique_names: List[str] = []
    for nm, _student in student_entries:
        key = name_key(nm)
        if not key or key in seen_name_keys:
            continue
        seen_name_keys.add(key)
        unique_names.append(nm)

    profile_id_by_name: Dict[str, str] = {}
    unmatched_names: List[str] = []
    try:
        enr = (
            supabase_admin.table("enrollments")
            .select("student_id, profiles(id, full_name)")
            .eq("class_id", class_id)
            .execute()
        )
        enrolled_profiles: List[Dict[str, Any]] = []
        for row in (enr.data or []):
            prof = normalize_profile(row)
            pid = str((prof or {}).get("id") or row.get("student_id") or "").strip()
            fn = str((prof or {}).get("full_name") or "").strip()
            if pid and fn:
                enrolled_profiles.append({"id": pid, "full_name": fn})
        enrolled_index = _enrolled_import_name_index(enrolled_profiles)
        for nm in unique_names:
            ids = enrolled_index.get(name_key(nm)) or []
            if len(ids) > 1:
                return jsonify({
                    "error": "ambiguous_name",
                    "name": nm,
                }), 409
            hit = _lookup_enrolled_import_student_id(nm, enrolled_index)
            if hit:
                profile_id_by_name[name_key(nm)] = hit
            else:
                unmatched_names.append(nm)
    except Exception as e:
        logger.error("Import roster name index failed for class %s: %s", class_id, e)
        return _safe_api_error("Could not load enrolled students", 500, log_detail=e)

    ignored_raw = data.get("ignored_names", [])
    if ignored_raw is None:
        ignored_raw = []
    if not isinstance(ignored_raw, list):
        return jsonify({"success": False, "error": "ignored_names must be a list"}), 400
    if len(ignored_raw) > MAX_IMPORT_ROWS:
        return jsonify({
            "success": False,
            "error": f"ignored_names has too many entries (max {MAX_IMPORT_ROWS})",
        }), 400
    ignored_keys: set = set()
    for item in ignored_raw:
        if not isinstance(item, str):
            return jsonify({
                "success": False,
                "error": "ignored_names entries must be strings",
            }), 400
        text = item.strip()
        if len(text) > 255:
            return jsonify({
                "success": False,
                "error": "ignored_names entries must be 255 characters or fewer",
            }), 400
        key = name_key(text)
        if key:
            ignored_keys.add(key)

    unresolved_names = [
        nm for nm in unmatched_names
        if name_key(nm) not in ignored_keys
    ]
    if unresolved_names:
        return jsonify({
            "success": False,
            "error": "Some names need confirmation",
            "unresolved_names": unresolved_names,
        }), 409

    try:
        los_resp = _select_class_pool_learning_objectives(
            "id,vendor_code,description", class_id
        )
        existing_los = los_resp.data or []
    except Exception as e:
        return _safe_api_error("Failed to load learning objectives", 500, log_detail=e)

    lo_map = {}
    for lo in existing_los:
        if lo.get('vendor_code'):
            lo_map[lo['vendor_code'].strip().lower()] = lo['id']
        if lo.get('description'):
            lo_map[lo['description'].strip().lower()] = lo['id']

    # Create missing LOs from extracted list
    for lo_name in extracted_los:
        if not lo_name or not str(lo_name).strip():
            continue
        key = lo_name.strip().lower()
        if key in lo_map:
            continue
        try:
            res = _insert_learning_objectives({
                "class_id": class_id,
                "vendor_code": lo_name,
                "description": None
            })
            if res.data:
                lo_map[key] = res.data[0]['id']
        except Exception as e:
            logger.error("Error creating LO '%s': %s", lo_name, e)

    imported = 0
    grade_rows = []
    to_clear_pairs: List[Dict[str, str]] = []
    skipped_hw_warnings: List[str] = []
    hw_storage_key = (
        Homework.resolve_hw_group_storage_key(class_id, assignment_id)
        if assignment_id
        else None
    )
    hw_map = (
        dict(Homework.get_hw_scores_map_for_assignment(str(class_id), str(assignment_id)) or {})
        if assignment_id
        else {}
    )
    stored_hw_map = dict(hw_map)
    incoming_hw: Dict[str, Dict[str, Any]] = {}

    # Link all extracted LOs to the selected assignment (if not already linked)
    if assignment_id:
        try:
            existing_links_resp = supabase_admin.table("assignment_objectives") \
                .select("learning_objective_id") \
                .eq("assignment_id", assignment_id) \
                .execute()
            already_linked = set(r["learning_objective_id"] for r in (existing_links_resp.data or []))

            new_links = []
            for lo_name in extracted_los:
                if not lo_name:
                    continue
                lo_id = lo_map.get(lo_name.strip().lower())
                if lo_id and lo_id not in already_linked:
                    new_links.append({"assignment_id": assignment_id, "learning_objective_id": lo_id})
                    already_linked.add(lo_id)
            if new_links:
                supabase_admin.table("assignment_objectives").insert(new_links).execute()
                logger.info("Batch-linked %d LOs to assignment %s", len(new_links), assignment_id)
        except Exception as e:
            logger.error("Error linking LOs to assignment: %s", e)

    for full_name, student in student_entries:
        profile_id = profile_id_by_name.get(name_key(full_name))
        if not profile_id:
            continue

        # Build grade rows to batch-upsert later
        grades = student.get('grades', {}) or {}
        if "homework_pct" in student and assignment_id and hw_storage_key:
            parsed_hw = Homework.parse_import_hw_pct(student.get("homework_pct"))
            sid_key = str(profile_id).strip()
            if parsed_hw is not None:
                incoming_hw[sid_key] = {
                    "student_id": profile_id,
                    "score_pct": parsed_hw,
                }
                hw_map[sid_key] = parsed_hw
        for lo_name, mark in grades.items():
            if not lo_name:
                continue

            lo_id = lo_map.get(lo_name.strip().lower())
            if not lo_id:
                continue

            mark_s = "" if mark is None else str(mark).strip()
            if not mark_s:
                if assignment_id:
                    to_clear_pairs.append(
                        {"student_id": profile_id, "lo_id": lo_id}
                    )
                continue

            normalized = Grade.normalize_score(mark_s)
            if not normalized:
                continue

            if (
                assignment_id
                and normalized != "A"
                and not Homework.student_has_recorded_score(hw_map, profile_id)
            ):
                skipped_hw_warnings.append(
                    f"Skipped: {full_name} - {lo_name} - no HW score recorded"
                )
                continue

            grade_rows.append({
                "student_id": profile_id,
                "learning_objective_id": lo_id,
                "top_score": normalized,
                "assignment_id": assignment_id,
                "last_modified_by": session['user_id'],
                "hw_score_at_entry": hw_map.get(str(profile_id).strip()),
            })

        imported += 1

    # PostgreSQL rejects one upsert statement containing the same conflict key
    # more than once. Reinsert duplicate keys so the last sheet row wins and
    # the resulting order reflects each surviving row's final occurrence.
    deduped_grade_rows: Dict[
        Tuple[Any, Any, Any], Dict[str, Any]
    ] = {}
    for row in grade_rows:
        conflict_key = (
            row.get("student_id"),
            row.get("learning_objective_id"),
            row.get("assignment_id"),
        )
        deduped_grade_rows.pop(conflict_key, None)
        deduped_grade_rows[conflict_key] = row
    grade_rows = list(deduped_grade_rows.values())

    # Batch upsert grades — overwrites existing scores for the same student+LO+assignment
    try:
        chunk_size = 150
        for i in range(0, len(grade_rows), chunk_size):
            chunk = grade_rows[i:i + chunk_size]
            supabase_admin.table("grades").upsert(
                chunk, on_conflict="student_id,learning_objective_id,assignment_id"
            ).execute()
            logger.info("Upserted %d grades", len(chunk))
    except Exception as e:
        return _safe_api_error("Could not import grades", 500, log_detail=e)

    hw_rows = []
    for sid_key, inc in incoming_hw.items():
        current = stored_hw_map[sid_key] if sid_key in stored_hw_map else None
        decision = Homework.decide_hw_score_update(current, inc["score_pct"])
        if decision["action"] == "unchanged":
            continue
        hw_rows.append({
            "student_id": inc["student_id"],
            "class_id": class_id,
            "homework_group": str(hw_storage_key).strip(),
            "score_pct": decision["score_pct"],
            "prev_score_pct": decision["prev_score_pct"],
        })

    # Persist homework % scores in homework_scores.
    try:
        if hw_rows:
            chunk_size = 150
            for i in range(0, len(hw_rows), chunk_size):
                chunk = hw_rows[i:i + chunk_size]
                supabase_admin.table("homework_scores").upsert(
                    chunk, on_conflict="student_id,class_id,homework_group"
                ).execute()
            logger.info("Upserted %d homework %% scores from import", len(hw_rows))
    except Exception as e:
        logger.error("Error processing homework scores in bulk: %s", e)

    if assignment_id and to_clear_pairs:
        _clear_grade_cells_for_assignment(
            str(class_id),
            str(assignment_id),
            to_clear_pairs,
            session.get("user_id"),
        )

    return jsonify({
        "success": True,
        "imported_students": imported,
        "imported_grades": len(grade_rows),
        "imported_hw_scores": len(hw_rows),
        "warnings": skipped_hw_warnings,
        "skipped_no_hw": len(skipped_hw_warnings),
    }), 200


@main_bp.route("/api/class/<class_id>/parse-grade-csv", methods=["POST"])
@api_instructor_required
@rate_limited("parse_grade_csv", limit=30, window_sec=900)
def api_parse_grade_csv(class_id):
    """Parse a Download Blank CSV gradesheet locally."""
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403

    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"success": False, "error": "No file provided"}), 400
    filename = str(upload.filename or "")
    if not filename.lower().endswith(".csv"):
        return jsonify({"success": False, "error": "File must be a CSV"}), 400

    content = upload.read()
    if len(content) > 10 * 1024 * 1024:
        return jsonify({"success": False, "error": "File is too large"}), 400
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return jsonify({
            "success": False,
            "error": "CSV must be UTF-8 encoded (the blank gradesheet export is UTF-8).",
        }), 400

    try:
        los = Course.get_learning_objectives(class_id) or []
    except Exception as e:
        return _safe_api_error("Could not load learning objectives", 500, log_detail=e)
    vendor_codes = [str(lo.get("vendor_code") or "") for lo in los]

    payload, err = parse_blank_gradesheet_csv_text(text, vendor_codes)
    if err:
        return jsonify({"success": False, "error": err}), 400

    assignment_name = payload.get("assignment_name") or ""
    needle = _normalize_assignment_title(assignment_name)
    try:
        asg_resp = (
            supabase_admin.table("assignments")
            .select("id, name")
            .eq("class_id", class_id)
            .execute()
        )
        matches = [
            row for row in (asg_resp.data or [])
            if _normalize_assignment_title(row.get("name")) == needle
        ]
    except Exception as e:
        return _safe_api_error("Could not match the assignment", 500, log_detail=e)

    if not matches:
        return jsonify({
            "success": False,
            "error": f"No assignment named '{assignment_name}' in this class.",
        }), 400

    ambiguous = len(matches) > 1
    matched = matches[0] if not ambiguous else {}
    return jsonify({
        "success": True,
        "matched_assignment_id": None if ambiguous else matched.get("id"),
        "matched_assignment_title": None if ambiguous else matched.get("name"),
        "assignment_match": "ambiguous" if ambiguous else "matched",
        "data": {
            "students": payload.get("students") or [],
            "learning_objectives": payload.get("learning_objectives") or [],
            "homework_column": payload.get("homework_column") or "HW",
            "extraction_path": "csv",
            "assignment_name": assignment_name,
            "date_value": payload.get("date_value") or "",
        },
    }), 200
