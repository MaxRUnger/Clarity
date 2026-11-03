import logging
from typing import Any, Dict, List, Tuple

from app.helpers.authentication import supabase_admin
from app.helpers.gradesheet_csv import _gradesheet_letter_map_from_rows
from app.helpers.paging import fetch_all_rows

logger = logging.getLogger(__name__)


def _gradesheet_letter_map_for_assignment(
    assignment_id: str,
    students: List[Dict[str, Any]],
    learning_objectives: List[Dict[str, Any]],
) -> Dict[Tuple[str, str], str]:
    letter_map: Dict[Tuple[str, str], str] = {}
    lo_ids = [str(lo.get("id") or "").strip() for lo in learning_objectives if lo.get("id")]
    student_ids = [str(s.get("id") or "").strip() for s in students if s.get("id")]
    if not lo_ids or not student_ids:
        return letter_map
    try:
        grade_rows: List[Dict[str, Any]] = []
        chunk = 100
        for i in range(0, len(student_ids), chunk):
            batch = student_ids[i:i + chunk]
            # One chunk of 100 students can still be more than 1000 grade rows.
            grade_rows.extend(fetch_all_rows(
                supabase_admin.table("grades")
                .select("id, student_id, learning_objective_id, top_score")
                .eq("assignment_id", assignment_id)
                .in_("student_id", batch)
                .in_("learning_objective_id", lo_ids)
                .order("id")
            ))
        letter_map = _gradesheet_letter_map_from_rows(grade_rows)
    except Exception as e:
        logger.error(
            "gradesheet export: failed to load grades for assignment %s: %s",
            assignment_id, e,
        )
        letter_map = {}
    return letter_map
