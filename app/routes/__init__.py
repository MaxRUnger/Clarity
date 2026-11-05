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

import logging
import os
from datetime import date
from typing import Any, Dict, List, Tuple
from flask import (  # type: ignore
    render_template,
    request,
    jsonify,
    redirect,
    url_for,
    session,
    Response,
)
from werkzeug.utils import secure_filename
from app.helpers.authentication import supabase_admin
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
from app.helpers.local_time import local_today
from app.helpers.calendar_dates import clean_assignment_dates
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
    row_sort_key,
)
from app.helpers.lo_import import (
    _insert_learning_objectives,
    _lo_vendor_code_conflict,
    _select_class_pool_learning_objectives,
    annotate_learning_objective_rows_for_preview,
    import_learning_objectives_rows,
)
from app.helpers.class_form import (
    CLASS_DAYS,
    _clean_class_days,
    _clean_class_form,
    _clean_section_number,
)
from app.helpers.lo_progress import (
    build_learning_objectives_grid,
    learning_objective_column_name,
)
from app.helpers.revision_report import build_revision_report
from app.helpers.revision_mail import (
    _attach_revision_messages,
    _public_reminder_student,
    _reminder_counts,
    _revision_class_title,
    _send_revision_reminders,
    revision_reminder_partition,
)
from app.helpers.revision_text import _revision_sender_name
from app.helpers.csv_text import (
    MAX_IMPORT_ROWS,
    _normalize_assignment_title,
    parse_blank_gradesheet_csv_text,
)
from app.helpers.form_tokens import _consume_one_time_form_token
from app.helpers.auth_decorators import (
    api_instructor_required,
    api_login_required,
    login_required,
)
from app.helpers.free_passes import (
    FREE_PASSES_ALLOWED,
    _batch_get_free_passes,
    _promote_non_counting_masteries_for_student,
)
from app.helpers.csv_upload import _parse_lo_csv_upload
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
    _student_enrolled_in_class,
)
from app.helpers.class_progress import (
    _process_enrollments,
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
from app.helpers.class_removal import delete_class_rows
from app.helpers.gradesheet_lookup import _gradesheet_letter_map_for_assignment
from app.helpers.assignment_rows import (
    _assignment_type_fields_from_payload,
    _insert_assignment_objectives,
)
from app.helpers.api_input import (
    _require_json_object,
    _safe_api_error,
)
from app.helpers.rate_limit import rate_limited
from uuid import uuid4

logger = logging.getLogger(__name__)

from app.routes.blueprint import main_bp

ROUTE_MODULES = (
    "app.routes.reports",
    "app.routes.auth",
    "app.routes.students",
    "app.routes.archives",
)

_HW_REQUIRED_FOR_MARK_ERROR = (
    "Enter a homework % for this student before saving a grade other than A (Absent)."
)


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
