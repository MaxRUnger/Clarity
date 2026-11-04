import logging
from typing import Any, Optional

from flask import session

from app.helpers.authentication import supabase_admin
from app.helpers.request_cache import _request_cache
from app.models import Course

logger = logging.getLogger(__name__)


def _class_auto_convert_m_enabled(class_id: str) -> bool:
    cache = _request_cache()
    ck = ("_class_auto_convert_m_enabled", str(class_id))
    if ck in cache:
        return cache[ck]
    val = False
    try:
        resp = (
            supabase_admin.table("classes")
            .select("auto_convert_m")
            .eq("id", class_id)
            .limit(1)
            .execute()
        )
        if resp.data:
            val = bool(resp.data[0].get("auto_convert_m"))
    except Exception as e:
        logger.error("Error reading auto_convert_m for class %s: %s", class_id, e)
    cache[ck] = val
    return val


def _class_instructor_id(class_id: str) -> Optional[str]:
    cache = _request_cache()
    ck = ("_class_instructor_id", str(class_id))
    if ck in cache:
        return cache[ck]
    val: Optional[str] = None
    try:
        resp = supabase_admin.table("classes").select("instructor_id").eq("id", class_id).limit(1).execute()
        if resp.data:
            raw = resp.data[0].get("instructor_id")
            val = raw if isinstance(raw, str) else None
    except Exception as e:
        logger.error("_class_instructor_id: %s", e)
    cache[ck] = val
    return val


def _owned_class_row(class_id, instructor_id):
    """The class row for this instructor, read fresh on every call."""
    resp = (
        supabase_admin.table("classes")
        .select("id, name, semester, days, section_number, instructor_id")
        .eq("id", class_id)
        .eq("instructor_id", instructor_id)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


def _user_ids_equal(a: Any, b: Any) -> bool:
    """Compare auth user ids / FK ids from Supabase (string case and whitespace tolerant)."""
    if a is None or b is None:
        return False
    sa = str(a).strip().lower()
    sb = str(b).strip().lower()
    if sa == sb:
        return True
    return sa.replace("-", "") == sb.replace("-", "")


def _allowed_lo_ids_for_grading(class_id, assignment_id=None) -> set:
    """LO ids that may be graded.

    When an assignment_id is given, only LOs actually linked to that
    assignment via assignment_objectives are allowed. A crafted request
    for a class LO not linked to the assignment is silently dropped before
    any DB write, matching the scoping the CSV export helpers already apply.
    When no assignment_id is given (unscoped grade entry), the full class
    LO pool is returned.
    """
    if assignment_id:
        return {str(x) for x in (Course.get_assignment_lo_ids(assignment_id) or [])}
    return {str(x) for x in (Course.get_lo_ids_for_class(class_id) or [])}


def _instructor_owns_class(class_id: str) -> bool:
    uid = session.get("user_id")
    owner = _class_instructor_id(class_id)
    return bool(uid and owner and _user_ids_equal(owner, uid))


def _assignment_belongs_to_class(class_id: str, assignment_id: str) -> bool:
    cache = _request_cache()
    ck = ("_assignment_belongs_to_class", str(class_id), str(assignment_id))
    if ck in cache:
        return cache[ck]
    val = False
    try:
        r = (
            supabase_admin.table("assignments")
            .select("id")
            .eq("id", assignment_id)
            .eq("class_id", class_id)
            .limit(1)
            .execute()
        )
        val = bool(r.data)
    except Exception:
        val = False
    cache[ck] = val
    return val


def _enrolled_student_ids_for_class(class_id: str) -> set:
    """Set of student_ids with an enrollment row in class_id."""
    cache = _request_cache()
    ck = ("_enrolled_student_ids_for_class", str(class_id))
    if ck in cache:
        return cache[ck]
    ids: set = set()
    try:
        resp = (
            supabase_admin.table("enrollments")
            .select("student_id")
            .eq("class_id", class_id)
            .execute()
        )
        ids = {str(r["student_id"]) for r in (resp.data or []) if r.get("student_id")}
    except Exception as e:
        logger.error("enrollment lookup failed for class %s: %s", class_id, e)
    cache[ck] = ids
    return ids


def _student_enrolled_in_class(class_id: str, student_id: str) -> bool:
    """True only if student_id has a real enrollment row in class_id.

    Fails closed (returns False) on lookup error or missing ids — grade/HW/
    exam/pass writes must never succeed for a student the DB can't confirm
    is actually on this class's roster.
    """
    if not class_id or not student_id:
        return False
    return str(student_id) in _enrolled_student_ids_for_class(class_id)
