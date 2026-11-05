import logging
from uuid import uuid4

from flask import jsonify, redirect, render_template, request, session, url_for

from app.helpers.api_input import _safe_api_error
from app.helpers.authentication import supabase_admin
from app.helpers.auth_decorators import api_instructor_required, login_required
from app.helpers.class_access import _instructor_owns_class, _student_enrolled_in_class
from app.helpers.class_data import _class_data_for_progress
from app.helpers.class_progress import (
    _process_enrollments,
    class_lo_lookup,
    class_progress,
    normalize_profile,
    objectives_attached_to_assignments,
)
from app.helpers.class_removal import (
    _filter_query,
    class_student_removal_counts,
    class_student_removal_filters,
)
from app.helpers.csv_upload import _parse_student_csv_upload
from app.helpers.enrollment import (
    _classify_student_csv_rows,
    _instructor_student_email_index,
    _profile_used_by_another_instructor,
    _student_csv_rejection,
    decide_student_enrollment,
    validate_student_name_and_email,
)
from app.helpers.grade_log import _log_grade_deletions
from app.helpers.paging import fetch_all_rows
from app.helpers.rate_limit import rate_limited
from app.helpers.report_email import _email_set_timestamp
from app.helpers.student_names import display_name, normalize_email
from app.models import Course
from app.routes.blueprint import main_bp

logger = logging.getLogger(__name__)

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

