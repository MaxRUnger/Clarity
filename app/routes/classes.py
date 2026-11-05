import logging
from datetime import date
from uuid import uuid4

from flask import jsonify, redirect, render_template, request, session, url_for

from app.helpers.api_input import _safe_api_error
from app.helpers.auth_decorators import api_instructor_required, login_required
from app.helpers.authentication import supabase_admin
from app.helpers.class_access import _instructor_owns_class
from app.helpers.class_data import (
    _class_data_for_progress,
    load_assignments_for_class,
    organize_by_learning_objectives,
)
from app.helpers.class_form import (
    CLASS_DAYS,
    _clean_class_days,
    _clean_class_form,
    _clean_section_number,
)
from app.helpers.class_progress import _process_enrollments
from app.helpers.class_removal import delete_class_rows
from app.helpers.form_tokens import _consume_one_time_form_token
from app.helpers.lo_import import (
    _insert_learning_objectives,
    _select_class_pool_learning_objectives,
)
from app.helpers.lo_progress import (
    build_learning_objectives_grid,
    learning_objective_column_name,
)
from app.helpers.local_time import local_today
from app.helpers.report_email import EMAIL_HOLD_MESSAGE
from app.helpers.revision_mail import revision_reminder_partition
from app.helpers.revision_report import build_revision_report
from app.helpers.student_names import display_name
from app.models import DEFAULT_REQUIRED_MS, Course, Grade
from app.routes.blueprint import main_bp

logger = logging.getLogger(__name__)

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
