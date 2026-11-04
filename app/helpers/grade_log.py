import logging
from typing import Any, Dict, List, Optional

from app.helpers.authentication import supabase_admin
from app.helpers.paging import fetch_all_rows

logger = logging.getLogger(__name__)


# INSERT and UPDATE audit rows are written by trg_log_grade_change
# (AFTER INSERT OR UPDATE on grades). That trigger copies
# NEW.last_modified_by into changed_by. Python writes DELETE only.
GRADE_LOG_INSERT = "INSERT"
GRADE_LOG_UPDATE = "UPDATE"
GRADE_LOG_DELETE = "DELETE"
GRADE_LOG_OPERATIONS = (GRADE_LOG_INSERT, GRADE_LOG_UPDATE, GRADE_LOG_DELETE)
_MISSING_GRADE_LOG_TABLE = frozenset({"42P01", "PGRST205"})


def _grade_log_pg_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if code:
        return str(code)
    if exc.args and isinstance(exc.args[0], dict):
        return str(exc.args[0].get("code") or "")
    return ""


def _write_grade_change_log(chunk: List[Dict[str, Any]]) -> None:
    try:
        supabase_admin.table("grade_change_log").insert(chunk).execute()
    except Exception as e:
        if _grade_log_pg_code(e) in _MISSING_GRADE_LOG_TABLE:
            logger.warning(
                "grade_change_log insert skipped (table may not exist yet): %s", e
            )
            return
        ops = sorted({str(row.get("operation") or "") for row in chunk})
        logger.error(
            "grade_change_log insert failed for operation %s: %s",
            ",".join(ops),
            e,
        )


def _linked_objective_id_set(linked_ids: List[Any]) -> set:
    return {
        str(raw).strip()
        for raw in (linked_ids or [])
        if str(raw or "").strip()
    }


def grades_outside_linked_objectives(
    assignment_id: str,
    linked_ids: List[Any],
) -> List[Dict[str, Any]]:
    """Grades on this assignment whose objective is outside the linked set.

    The detach count and the grade delete both use this list, so the number
    shown and the number deleted stay the same.
    """
    keep = _linked_objective_id_set(linked_ids)
    assignment_key = str(assignment_id)
    rows = fetch_all_rows(
        supabase_admin.table("grades")
        .select(
            "id, student_id, learning_objective_id, assignment_id, top_score, "
            "second_score, counts_for_mastery"
        )
        .eq("assignment_id", assignment_key)
        .order("id")
    )
    matched = []
    for row in rows:
        row_assignment = str(row.get("assignment_id") or "").strip()
        if row_assignment and row_assignment != assignment_key:
            continue
        lo_id = str(row.get("learning_objective_id") or "").strip()
        if lo_id not in keep:
            matched.append(row)
    return matched


def delete_grades_outside_linked_objectives(
    class_id: str,
    assignment_id: str,
    linked_ids: List[Any],
    changed_by: Optional[str],
) -> int:
    """Delete grades on this assignment whose objective is outside linked_ids.

    An empty linked set deletes every grade with this assignment_id.
    Grades on other assignments are left in place. homework_scores are not read
    or written. The audit log is written before the delete.
    """
    rows = grades_outside_linked_objectives(assignment_id, linked_ids)
    if not rows:
        return 0
    _log_grade_deletions(rows, class_id, changed_by)
    delete_q = supabase_admin.table("grades").delete().eq(
        "assignment_id", str(assignment_id)
    )
    keep = _linked_objective_id_set(linked_ids)
    if keep:
        lo_ids = []
        seen = set()
        for row in rows:
            lo_id = str(row.get("learning_objective_id") or "").strip()
            if lo_id and lo_id not in seen:
                seen.add(lo_id)
                lo_ids.append(lo_id)
        if lo_ids:
            delete_q = delete_q.in_("learning_objective_id", lo_ids)
    delete_q.execute()
    return len(rows)


def _log_grade_deletions(rows: List[Dict[str, Any]], class_id: str, changed_by: Optional[str]) -> None:
    """Best-effort audit log for grade rows about to be hard-deleted.

    Reads pre-delete values so grade_change_log retains history even though the
    grades table itself has no soft-delete. Must never raise. A missing audit
    table is a warning. Any other insert error is logged and swallowed so the
    delete still proceeds.
    """
    if not rows:
        return
    log_rows = [
        {
            "student_id": r.get("student_id"),
            "class_id": class_id,
            "learning_objective_id": r.get("learning_objective_id"),
            "assignment_id": r.get("assignment_id"),
            "operation": GRADE_LOG_DELETE,
            "old_value": {
                "top_score": r.get("top_score"),
                "second_score": r.get("second_score"),
                "counts_for_mastery": r.get("counts_for_mastery"),
            },
            "new_value": None,
            "changed_by": changed_by,
        }
        for r in rows
    ]
    # Chunk large batches (e.g. delete_class on a big roster) so one insert
    # call doesn't hit request/payload limits, matching api_import_grades.
    chunk_size = 150
    for i in range(0, len(log_rows), chunk_size):
        _write_grade_change_log(log_rows[i:i + chunk_size])


def _clear_grade_cells_for_assignment(
    class_id: str,
    assignment_id: Optional[str],
    pairs: List[Dict[str, str]],
    changed_by: Optional[str],
) -> int:
    """Delete existing grade rows for student+LO pairs on this assignment.

    Same path as SpeedGrader empty-cell saves: pre-fetch, audit-log, delete.
    Pairs with no matching row are a silent no-op.
    """
    if not pairs:
        return 0
    clear_sids = list({str(it["student_id"]) for it in pairs if it.get("student_id")})
    clear_lo_ids = list({str(it["lo_id"]) for it in pairs if it.get("lo_id")})
    clear_keys = {
        f"{it['student_id']}|{it['lo_id']}"
        for it in pairs
        if it.get("student_id") and it.get("lo_id")
    }
    if not clear_sids or not clear_lo_ids or not clear_keys:
        return 0
    cleared_rows: List[Dict[str, Any]] = []
    try:
        clear_q = supabase_admin.table("grades").select(
            "student_id, learning_objective_id, assignment_id, top_score, second_score, counts_for_mastery"
        ).in_("student_id", clear_sids).in_("learning_objective_id", clear_lo_ids)
        clear_q = (
            clear_q.eq("assignment_id", assignment_id)
            if assignment_id
            else clear_q.is_("assignment_id", "null")
        )
        clear_resp = clear_q.execute()
        for r in (clear_resp.data or []):
            k = f"{r.get('student_id')}|{r.get('learning_objective_id')}"
            if k in clear_keys:
                cleared_rows.append(r)
    except Exception as e:
        logger.error("clear_grade_cells pre-fetch failed: %s", e)
        return 0

    if not cleared_rows:
        return 0
    _log_grade_deletions(cleared_rows, class_id, changed_by)
    for r in cleared_rows:
        try:
            del_q = (
                supabase_admin.table("grades").delete()
                .eq("student_id", r["student_id"])
                .eq("learning_objective_id", r["learning_objective_id"])
            )
            del_q = (
                del_q.eq("assignment_id", assignment_id)
                if assignment_id
                else del_q.is_("assignment_id", "null")
            )
            del_q.execute()
        except Exception as e:
            logger.error(
                "failed to clear grade for student=%s lo=%s: %s",
                r.get("student_id"), r.get("learning_objective_id"), e,
            )
    return len(cleared_rows)
