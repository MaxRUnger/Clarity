import logging
from typing import Any, Dict, List, Optional

from app.helpers.authentication import supabase_admin
from app.helpers.class_progress import (
    _lo_display_title,
    _process_enrollments,
    objectives_attached_to_assignments,
)
from app.helpers.lo_order import lo_sort_key
from app.helpers.student_names import display_name, row_sort_key
from app.models import Course, Grade, Homework

logger = logging.getLogger(__name__)


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def organize_by_learning_objectives(students, learning_objectives):
    """Maps student grades to the relevant Learning Objectives for the UI."""
    lo_dict = {str(lo['id']): {
        'id': str(lo['id']),
        'name': (lo.get('name') or _lo_display_title(lo)),
        'students_with_2m': [],
        'students_with_1m': [],
        'students_with_0m': [],
        'total_students': len(students)
    } for lo in learning_objectives}

    for student in students:
        student_grades = student.get('grades', [])
        for grade in student_grades:
            lo_id = str(grade['learning_objective_id'])
            if lo_id in lo_dict:
                m_count = 0
                top = grade.get('top_score')
                sec = grade.get('second_score')

                if Grade.is_mastery_mark(top):
                    m_count += 1
                if Grade.is_mastery_mark(sec):
                    m_count += 1

                raw_fn = student.get("full_name") or ""
                student_data = {
                    "id": student["id"],
                    "full_name": raw_fn,
                    "name": student.get("name")
                    or display_name(raw_fn),
                    "top_score": top,
                    "second_score": sec,
                }

                if m_count == 2:
                    lo_dict[lo_id]['students_with_2m'].append(student_data)
                elif m_count == 1:
                    lo_dict[lo_id]['students_with_1m'].append(student_data)
                else:
                    lo_dict[lo_id]['students_with_0m'].append(student_data)

    for lo in lo_dict.values():
        for col in ("students_with_2m", "students_with_1m", "students_with_0m"):
            lo[col].sort(key=row_sort_key)

    return list(lo_dict.values())


def _learning_objectives_for_assignment(class_id: str, assignment_id: str) -> List[Dict[str, Any]]:
    """Class LO pool filtered to rows in assignment_objectives for this assignment."""
    try:
        pool = Course.get_learning_objectives(class_id) or []
    except Exception as e:
        logger.error("gradesheet export: failed to load LOs for class %s: %s", class_id, e)
        pool = []
    linked = {
        str(lo_id).strip()
        for lo_id in (Course.get_assignment_lo_ids(assignment_id) or [])
        if str(lo_id).strip()
    }
    if not linked:
        return []
    return [
        lo for lo in pool
        if str(lo.get("id") or "").strip() in linked
    ]


def _gradesheet_export_bundle(class_id: str, assignment_id: str):
    """Assignment row, roster, LOs, HW map. None if the assignment is missing."""
    try:
        assignment_result = (
            supabase_admin.table("assignments")
            .select("id, name, date_returned")
            .eq("id", assignment_id)
            .eq("class_id", class_id)
            .limit(1)
            .execute()
        )
        assignment = (assignment_result.data or [None])[0]
    except Exception as e:
        logger.error("gradesheet export: failed to load assignment %s: %s", assignment_id, e)
        assignment = None
    if not assignment:
        return None

    class_data = Course.get_full_class_data(class_id)
    students, _, _ = _process_enrollments(class_data) if class_data else ([], [], {})
    learning_objectives = _learning_objectives_for_assignment(class_id, assignment_id)

    hw_map = Homework.get_hw_scores_map_for_assignment(class_id, assignment_id)
    return assignment, students, learning_objectives, hw_map


def _class_data_for_progress(class_data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Copy class data whose objective list is the attached set when the class has an id."""
    if not class_data or not class_data.get("id"):
        return class_data
    progress_data = dict(class_data)
    progress_data["learning_objectives"] = objectives_attached_to_assignments(
        class_data.get("id"),
        class_data.get("learning_objectives") or [],
    )
    return progress_data


# We deliberately enumerate columns instead of `select("*")` so adding new
# columns later doesn't accidentally widen the payload for every page that
# loads assignments.
_ASSIGNMENT_COLS = (
    "id, class_id, name, homework_group, date_returned, revision_due, created_at, "
    "assignment_type, "
    "assignment_objectives!assignment_objectives_assignment_id_fkey("
    "learning_objective_id, "
    "learning_objectives(id, vendor_code, description))"
)


def load_assignments_for_class(class_id, desc=False):
    """Load assignments with linked LOs for a class.

    Centralizes the repeated assignment query used by multiple route handlers and
    selects only the columns templates/APIs read.

    Returns:
        list of assignment dicts (empty list on error).
    """
    try:
        assignments_result = (
            supabase_admin.table("assignments")
            .select(_ASSIGNMENT_COLS)
            .eq("class_id", class_id)
            .order("created_at", desc=desc)
            .execute()
        )
        rows = assignments_result.data or []
        for assignment in rows:
            links = assignment.get("assignment_objectives")
            if isinstance(links, list):
                links.sort(key=lo_sort_key)
        return rows
    except Exception as e:
        logger.error("Error loading assignments for class %s: %s", class_id, e)
        return []
