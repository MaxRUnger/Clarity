import logging
from typing import Any, Dict, List, Tuple

from flask import jsonify, redirect, render_template, request, session, url_for

from app.helpers.api_input import _safe_api_error
from app.helpers.auth_decorators import (
    api_instructor_required,
    api_login_required,
    login_required,
)
from app.helpers.authentication import supabase_admin
from app.helpers.class_access import (
    _allowed_lo_ids_for_grading,
    _assignment_belongs_to_class,
    _class_auto_convert_m_enabled,
    _enrolled_student_ids_for_class,
    _instructor_owns_class,
    _student_enrolled_in_class,
)
from app.helpers.class_data import load_assignments_for_class
from app.helpers.class_progress import normalize_profile
from app.helpers.csv_text import (
    MAX_IMPORT_ROWS,
    _normalize_assignment_title,
    parse_blank_gradesheet_csv_text,
)
from app.helpers.free_passes import (
    FREE_PASSES_ALLOWED,
    _batch_get_free_passes,
    _demote_pass_promoted_masteries_for_student,
    _promote_non_counting_masteries_for_student,
)
from app.helpers.grade_log import _clear_grade_cells_for_assignment
from app.helpers.gradesheet_csv import (
    _enrolled_import_name_index,
    _lookup_enrolled_import_student_id,
)
from app.helpers.lo_import import (
    _insert_learning_objectives,
    _select_class_pool_learning_objectives,
)
from app.helpers.rate_limit import rate_limited
from app.helpers.student_names import display_name, name_key, row_sort_key
from app.models import ASSIGNMENT_TYPES, Course, Grade, Homework
from app.routes.blueprint import main_bp

logger = logging.getLogger(__name__)

_HW_REQUIRED_FOR_MARK_ERROR = (
    "Enter a homework % for this student before saving a grade other than A (Absent)."
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
            promoted = _promote_non_counting_masteries_for_student(class_id, student_id, assignment_id)

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

        demoted_lo_ids = []
        if existing.data and existing.data[0]['passes_used'] > 0:
            current_used = existing.data[0]['passes_used']
            supabase_admin.table("free_passes") \
                .update({"passes_used": current_used - 1}) \
                .eq("id", existing.data[0]['id']) \
                .execute()
            remaining = passes_allowed - (current_used - 1)
            assignment_id = str((data or {}).get("assignment_id") or "").strip()
            if assignment_id and _assignment_belongs_to_class(class_id, assignment_id):
                demoted_lo_ids = _demote_pass_promoted_masteries_for_student(
                    class_id, str(student_id), assignment_id
                )
        else:
            remaining = passes_allowed

        return jsonify({
            "success": True,
            "passes_remaining": remaining,
            "demoted_lo_ids": demoted_lo_ids,
        })
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
