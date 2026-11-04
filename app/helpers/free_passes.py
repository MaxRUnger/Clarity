from app.helpers.authentication import supabase_admin
from app.models import Course


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
    except Exception:
        return {}


def _promote_non_counting_masteries_for_student(class_id: str, student_id: str, changed_by: str = None) -> int:
    """When a student uses an HW pass, restore all their non-counting M/MR rows."""
    lo_ids = Course.get_all_lo_ids_for_class(class_id)
    if not lo_ids:
        return 0
    resp = supabase_admin.table("grades").select(
        "student_id, learning_objective_id, assignment_id, top_score, counts_for_mastery"
    ).eq("student_id", student_id).in_("learning_objective_id", lo_ids).in_(
        "top_score", ["M", "MR"]
    ).execute()

    rows = []
    for grade in resp.data or []:
        if grade.get("counts_for_mastery") is not False:
            continue
        row = {
            "student_id": student_id,
            "learning_objective_id": grade["learning_objective_id"],
            "top_score": grade["top_score"],
            "counts_for_mastery": True,
        }
        if changed_by is not None:
            row["last_modified_by"] = changed_by
        aid = grade.get("assignment_id")
        if aid is not None:
            row["assignment_id"] = aid
        rows.append(row)

    if not rows:
        return 0

    supabase_admin.table("grades").upsert(
        rows, on_conflict="student_id,learning_objective_id,assignment_id"
    ).execute()
    return len(rows)
