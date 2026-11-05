import logging
from typing import Any, Dict, List

from flask import redirect, render_template, url_for

from app.helpers.authentication import supabase_admin
from app.helpers.auth_decorators import login_required
from app.helpers.class_access import _instructor_owns_class
from app.helpers.class_data import _class_data_for_progress, load_assignments_for_class
from app.helpers.class_progress import (
    _process_enrollments,
    normalize_profile,
    objectives_attached_to_assignments,
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
from app.helpers.paging import fetch_all_rows
from app.helpers.report_email import _report_sender_name, instructor_title
from app.helpers.student_names import display_name
from app.models import Course
from app.routes.blueprint import main_bp

logger = logging.getLogger(__name__)

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

