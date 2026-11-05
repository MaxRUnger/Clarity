import logging
from datetime import date
from typing import Any, Dict, List

from flask import Response, jsonify, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename

from app.helpers.api_input import _require_json_object, _safe_api_error
from app.helpers.assignment_rows import (
    _assignment_type_fields_from_payload,
    _insert_assignment_objectives,
)
from app.helpers.auth_decorators import api_instructor_required, login_required
from app.helpers.authentication import supabase_admin
from app.helpers.calendar_dates import clean_assignment_dates
from app.helpers.class_access import _assignment_belongs_to_class, _instructor_owns_class
from app.helpers.class_data import _gradesheet_export_bundle, load_assignments_for_class
from app.helpers.class_progress import objectives_attached_to_assignments
from app.helpers.csv_text import MAX_IMPORT_ROWS
from app.helpers.csv_upload import _parse_lo_csv_upload
from app.helpers.grade_log import (
    _log_grade_deletions,
    delete_grades_outside_linked_objectives,
    grades_outside_linked_objectives,
)
from app.helpers.gradesheet_csv import _build_gradesheet_csv_text, _gradesheet_csv_data_rows
from app.helpers.gradesheet_lookup import _gradesheet_letter_map_for_assignment
from app.helpers.lo_import import (
    _insert_learning_objectives,
    _lo_vendor_code_conflict,
    annotate_learning_objective_rows_for_preview,
    import_learning_objectives_rows,
)
from app.helpers.paging import fetch_all_rows
from app.models import DEFAULT_REQUIRED_MS, Course, Homework
from app.routes.blueprint import main_bp

logger = logging.getLogger(__name__)

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
