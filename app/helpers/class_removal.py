from typing import Any, Dict, List, Optional

from app.helpers.authentication import supabase_admin
from app.helpers.grade_log import _log_grade_deletions
from app.helpers.paging import fetch_all_rows
from app.models import Course


def _filter_query(query, filters):
    for op, column, value in filters:
        if op == "eq":
            query = query.eq(column, value)
        else:
            query = query.in_(column, value)
    return query


def class_student_removal_filters(class_id, student_id):
    """Shared filters for the count and the delete, in delete order."""
    lo_ids = list(Course.get_all_lo_ids_for_class(class_id) or [])
    filters = []
    if lo_ids:
        filters.append((
            "grades",
            (
                ("eq", "student_id", student_id),
                ("in", "learning_objective_id", lo_ids),
            ),
        ))
    filters.append((
        "homework_scores",
        (("eq", "class_id", class_id), ("eq", "student_id", student_id)),
    ))
    filters.append((
        "free_passes",
        (("eq", "class_id", class_id), ("eq", "student_id", student_id)),
    ))
    filters.append((
        "enrollments",
        (("eq", "class_id", class_id), ("eq", "student_id", student_id)),
    ))
    return filters


def delete_class_rows(class_id: str, changed_by: Optional[str]) -> None:
    """Delete one class and its rows. The classes row is always last.

    Raises on any lookup or delete error. The grade audit log stays best effort.
    """
    def id_list(table: str) -> List[str]:
        rows = fetch_all_rows(
            supabase_admin.table(table).select("id").eq("class_id", class_id).order("id")
        )
        return [str(row["id"]) for row in rows if row.get("id")]

    def chunks(values: List[str]) -> List[List[str]]:
        return [values[index:index + 100] for index in range(0, len(values), 100)]

    def delete_in(table: str, column: str, values: List[str]) -> None:
        for group in chunks(values):
            supabase_admin.table(table).delete().in_(column, group).execute()

    objective_ids = id_list("learning_objectives")
    assignment_ids = id_list("assignments")
    grade_columns = (
        "id, student_id, learning_objective_id, assignment_id, "
        "top_score, second_score, counts_for_mastery"
    )
    found: List[Dict[str, Any]] = []
    for column, values in (
        ("assignment_id", assignment_ids),
        ("learning_objective_id", objective_ids),
    ):
        for group in chunks(values):
            found.extend(fetch_all_rows(
                supabase_admin.table("grades").select(grade_columns).in_(column, group).order("id")
            ))
    seen = set()
    grades = []
    for row in found:
        grade_id = row.get("id")
        if grade_id in seen:
            continue
        if grade_id is not None:
            seen.add(grade_id)
        grades.append(row)
    _log_grade_deletions(grades, class_id, changed_by)
    delete_in("grades", "assignment_id", assignment_ids)
    delete_in("grades", "learning_objective_id", objective_ids)
    supabase_admin.table("homework_scores").delete().eq("class_id", class_id).execute()
    supabase_admin.table("free_passes").delete().eq("class_id", class_id).execute()
    delete_in("assignment_objectives", "assignment_id", assignment_ids)
    delete_in("assignment_objectives", "learning_objective_id", objective_ids)
    supabase_admin.table("assignments").delete().eq("class_id", class_id).execute()
    supabase_admin.table("learning_objectives").delete().eq("class_id", class_id).execute()
    supabase_admin.table("enrollments").delete().eq("class_id", class_id).execute()
    supabase_admin.table("classes").delete().eq("id", class_id).execute()


def class_student_removal_counts(class_id, student_id):
    filters = class_student_removal_filters(class_id, student_id)
    counts = {"grades": 0, "homework_scores": 0, "free_passes": 0, "enrollments": 0}
    for table, scope in filters:
        query = supabase_admin.table(table).select("id", count="exact", head=True)
        resp = _filter_query(query, scope).execute()
        counts[table] = resp.count or 0
    return counts
