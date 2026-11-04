from typing import Any, Dict, List

from app.helpers.authentication import supabase_admin
from app.helpers.paging import fetch_all_rows

_HISTORY_PACKET_COLS = (
    "student_id, learning_objective_id, assignment_id, top_score, second_score"
)
_HISTORY_GRADE_COLS = (
    "learning_objective_id, assignment_id, top_score, second_score, assignments(name)"
)
_HISTORY_LO_CHUNK = 100
_HISTORY_PRINT_ERROR = "Could not load student history. Please try again."


def _history_objective_ids(learning_objectives) -> List[str]:
    ids: List[str] = []
    seen = set()
    for lo in learning_objectives or []:
        lo_id = str(lo.get("id") or "").strip()
        if not lo_id or lo_id in seen:
            continue
        seen.add(lo_id)
        ids.append(lo_id)
    return ids


def _grades_for_this_class(rows, assignments) -> List[Dict[str, Any]]:
    allowed = {
        str(assignment.get("id"))
        for assignment in assignments or []
        if assignment.get("id")
    }
    kept = []
    for grade in rows or []:
        raw = grade.get("assignment_id")
        if raw is None or str(raw) in allowed:
            kept.append(grade)
    return kept


def _history_packet_grades(lo_ids: List[str]) -> List[Dict[str, Any]]:
    """Every grade for these objectives, in id order. One page-set per 100 ids."""
    ids: List[str] = []
    seen = set()
    for raw in lo_ids:
        lo_id = str(raw or "").strip()
        if not lo_id or lo_id in seen:
            continue
        seen.add(lo_id)
        ids.append(lo_id)
    if not ids:
        return []
    rows: List[Dict[str, Any]] = []
    for start in range(0, len(ids), _HISTORY_LO_CHUNK):
        chunk = ids[start:start + _HISTORY_LO_CHUNK]
        rows.extend(fetch_all_rows(
            supabase_admin.table("grades")
            .select(_HISTORY_PACKET_COLS)
            .in_("learning_objective_id", chunk)
            .order("id")
        ))
    return rows


def student_history_sheet(learning_objectives, assignments, student_grades):
    """One student's grades in class assignment order, plus print rows.

    Grade lists follow the class assignment list, then grades whose assignment
    is not in that list, then grades with a null assignment_id. Duplicates stay
    in the order they were loaded. Print rows are class assignments that have
    a grade. Each print row's cells are the objective texts in that same order.
    """
    shown_lo_ids = {str(lo.get('id')) for lo in learning_objectives}
    class_assignment_ids = {str(assignment.get('id')) for assignment in assignments}
    grades_by_pair = {}
    outside_by_lo = {}
    unscoped_by_lo = {}
    for grade in student_grades:
        raw_lo = grade.get('learning_objective_id')
        if raw_lo is None:
            continue
        lo_id = str(raw_lo)
        if lo_id not in shown_lo_ids:
            continue
        # save_grades and api_import_grades store a null assignment_id when
        # the request omits one. Legacy rows are the same case.
        if grade.get('assignment_id') is None:
            unscoped_by_lo.setdefault(lo_id, []).append(grade)
            continue
        aid = str(grade.get('assignment_id'))
        if aid not in class_assignment_ids:
            outside_by_lo.setdefault(lo_id, []).append(grade)
            continue
        grades_by_pair.setdefault((lo_id, aid), []).append(grade)

    lo_grade_data = []
    for lo in learning_objectives:
        lo_id = str(lo.get('id'))
        grades = []
        for assignment in assignments:
            aid = str(assignment.get('id'))
            grades.extend(grades_by_pair.get((lo_id, aid), []))
        # Covers a failed or partial assignment load.
        grades.extend(outside_by_lo.get(lo_id, []))
        grades.extend(unscoped_by_lo.get(lo_id, []))
        lo_info = {
            'id': lo_id,
            'vendor_code': lo.get('vendor_code'),
            'description': lo.get('description'),
            'grades': grades
        }
        lo_grade_data.append(lo_info)

    used_assignment_ids = {pair[1] for pair in grades_by_pair}
    history_assignments = []
    for assignment in assignments:
        aid = str(assignment.get('id'))
        if aid not in used_assignment_ids:
            continue
        cells = []
        for lo in learning_objectives:
            lo_id = str(lo.get('id'))
            scores = []
            for grade in grades_by_pair.get((lo_id, aid), []):
                top_score = grade.get('top_score')
                second_score = grade.get('second_score')
                if top_score:
                    scores.append(top_score)
                if second_score:
                    scores.append(second_score)
            cells.append(", ".join(scores) if scores else "—")
        printed = dict(assignment)
        printed['cells'] = cells
        history_assignments.append(printed)
    return lo_grade_data, history_assignments
