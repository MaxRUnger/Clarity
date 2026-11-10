import logging

from app.helpers.authentication import supabase_admin

logger = logging.getLogger(__name__)

FREE_PASSES_ALLOWED = 2


def _batch_get_free_passes(student_ids, class_id):
    """Batch-fetch passes_used for a list of students. Returns {student_id: passes_used}."""
    if not student_ids:
        return {}
    try:
        resp = supabase_admin.table("free_passes") \
            .select("student_id, passes_used") \
            .eq("class_id", class_id) \
            .in_("student_id", student_ids) \
            .execute()
        return {r['student_id']: r['passes_used'] for r in (resp.data or [])}
    except Exception as exc:
        logger.error("free pass lookup failed for class %s: %s", class_id, exc)
        return {}


def _assignment_ids_sharing_homework_group(class_id: str, assignment_id: str) -> list:
    assignment_id = str(assignment_id or "").strip()
    class_id = str(class_id or "").strip()
    if not assignment_id or not class_id:
        return []
    resp = (
        supabase_admin.table("assignments")
        .select("id, homework_group")
        .eq("id", assignment_id)
        .eq("class_id", class_id)
        .limit(1)
        .execute()
    )
    if not resp.data:
        return []
    label = (resp.data[0].get("homework_group") or "").strip()
    if not label:
        return [assignment_id]
    siblings = (
        supabase_admin.table("assignments")
        .select("id")
        .eq("class_id", class_id)
        .eq("homework_group", label)
        .execute()
    )
    ids = []
    seen = set()
    for row in siblings.data or []:
        aid = str(row.get("id") or "").strip()
        if aid and aid not in seen:
            seen.add(aid)
            ids.append(aid)
    if assignment_id not in seen:
        ids.append(assignment_id)
    return ids


def _promote_non_counting_masteries_for_student(class_id: str, student_id: str, assignment_id: str) -> int:
    """Promote non-counting M/MR rows for this student on this homework group."""
    ids = _assignment_ids_sharing_homework_group(class_id, assignment_id)
    if not ids:
        return 0
    found = (
        supabase_admin.table("grades")
        .select("id")
        .eq("student_id", student_id)
        .in_("assignment_id", ids)
        .in_("top_score", ["M", "MR"])
        .eq("counts_for_mastery", False)
        .execute()
    )
    matched = found.data or []
    if not matched:
        return 0
    (
        supabase_admin.table("grades")
        .update({"counts_for_mastery": True, "promoted_by_pass": True})
        .eq("student_id", student_id)
        .in_("assignment_id", ids)
        .in_("top_score", ["M", "MR"])
        .eq("counts_for_mastery", False)
        .execute()
    )
    return len(matched)


def _demote_pass_promoted_masteries_for_student(class_id: str, student_id: str, assignment_id: str) -> list:
    """Return active-assignment LO ids demoted. The write covers the whole group."""
    ids = _assignment_ids_sharing_homework_group(class_id, assignment_id)
    if not ids:
        return []
    found = (
        supabase_admin.table("grades")
        .select("learning_objective_id, assignment_id")
        .eq("student_id", student_id)
        .in_("assignment_id", ids)
        .in_("top_score", ["M", "MR"])
        .eq("counts_for_mastery", True)
        .eq("promoted_by_pass", True)
        .execute()
    )
    rows = found.data or []
    if not rows:
        return []
    (
        supabase_admin.table("grades")
        .update({"counts_for_mastery": False, "promoted_by_pass": None})
        .eq("student_id", student_id)
        .in_("assignment_id", ids)
        .in_("top_score", ["M", "MR"])
        .eq("counts_for_mastery", True)
        .eq("promoted_by_pass", True)
        .execute()
    )
    active = str(assignment_id)
    return [
        str(row["learning_objective_id"])
        for row in rows
        if str(row.get("assignment_id") or "") == active and row.get("learning_objective_id")
    ]
