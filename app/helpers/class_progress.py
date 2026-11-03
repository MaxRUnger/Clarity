from typing import Any, Callable, Dict, List, Optional

from app.helpers.authentication import supabase_admin
from app.helpers.lo_order import lo_sort_key
from app.helpers.lo_progress import lo_cell_status
from app.helpers.paging import fetch_all_rows
from app.helpers.request_cache import _request_cache
from app.helpers.student_names import display_name, row_sort_key
from app.models import DEFAULT_REQUIRED_MS, Grade


def _merge_lo_row(lo_lookup, lo_id, embed):
    """Merge canonical LO metadata with an optional grade-row embed (fills gaps)."""
    base = dict(lo_lookup.get(lo_id) or {})
    emb = embed if isinstance(embed, dict) else {}
    for key in ("vendor_code", "description", "required_ms"):
        v = emb.get(key)
        if v is not None and v != "" and (key not in base or base.get(key) in (None, "")):
            base[key] = v
    return base


def _lo_display_title(lo_info):
    """Display objective identifier in UI lists."""
    vc = (lo_info.get("vendor_code") or "").strip()
    return vc or "Unknown LO"


def normalize_profile(enrollment):
    prof = enrollment.get('profiles', {})
    if isinstance(prof, list):
        prof = prof[0] if prof else {}
    return prof or {}


def _aggregate_lo_grades(
    raw_grades,
    lo_lookup,
    mastery_row_allowed: Optional[Callable[[Dict[str, Any]], bool]] = None,
):
    """Aggregate grades per LO across assignments.

    With per-assignment grading, a student may have multiple grade rows for the
    same LO (one per assignment).  This helper groups them and counts total M's
    so the student detail page can show e.g. "2 / 2 Ms".
    """
    lo_grades = {}
    allowed_ids = set(lo_lookup.keys()) if isinstance(lo_lookup, dict) else None
    for grade in (raw_grades or []):
        lo_id = str(grade.get('learning_objective_id')) if grade.get('learning_objective_id') else None
        if not lo_id:
            continue
        if allowed_ids is not None and len(allowed_ids) > 0 and lo_id not in allowed_ids:
            continue
        if lo_id not in lo_grades:
            lo_info = _merge_lo_row(lo_lookup, lo_id, grade.get("learning_objectives"))
            lo_grades[lo_id] = {
                'learning_objective_id': lo_id,
                'name': _lo_display_title(lo_info),
                'vendor_code': (lo_info.get('vendor_code') or '').strip(),
                'required_ms': lo_info.get('required_ms') or DEFAULT_REQUIRED_MS,
                'm_count': 0,
                'mr_count': 0,
                'grades_list': [],
                'grades_meta': [],
            }
        top = grade.get('top_score')
        # New persistent flag (scripts/add_grades_counts_for_mastery.sql) wins;
        # legacy rows missing the column default to True so existing M's still
        # count. Optional callback retained for compatibility (used by tests
        # and any caller layering extra constraints on top of the persisted
        # flag); it can only further restrict, never re-enable.
        row_counts_persistent = grade.get('counts_for_mastery')
        if row_counts_persistent is None:
            row_counts_persistent = True
        row_counts_persistent = bool(row_counts_persistent)
        row_counts_callback = True
        if mastery_row_allowed is not None:
            try:
                row_counts_callback = bool(mastery_row_allowed(grade))
            except Exception:
                row_counts_callback = True
        row_counts = row_counts_persistent and row_counts_callback
        if Grade.is_mastery_mark(top) and row_counts:
            lo_grades[lo_id]['m_count'] += 1
        if top == 'MR' and row_counts:
            lo_grades[lo_id]['mr_count'] += 1
        lo_grades[lo_id]['grades_list'].append(top)
        lo_grades[lo_id]['grades_meta'].append({
            'score': top,
            'counts_for_mastery': row_counts,
        })

    results = []
    for lo in lo_grades.values():
        lo['is_passed'] = lo_cell_status(lo['m_count'], lo['required_ms']) == "passed"
        lo['top_score'] = lo['grades_list'][0] if lo['grades_list'] else None
        lo['second_score'] = lo['grades_list'][1] if len(lo['grades_list']) > 1 else None
        # Mirror the same indices into grades_meta so templates can render a
        # red "non-counting" outline on each badge without a second lookup.
        meta = lo['grades_meta']
        lo['top_meta'] = meta[0] if meta else None
        lo['second_meta'] = meta[1] if len(meta) > 1 else None
        results.append(lo)
    results.sort(key=lo_sort_key)
    return results


def class_lo_lookup(pool):
    """Map objective id to objective for one class's pool (objectives without an id are skipped)."""
    return {str(lo["id"]): lo for lo in (pool or []) if lo.get("id")}


def _attached_objective_ids(class_id: str) -> set:
    """Objective ids linked to at least one assignment in this class.

    One paged read of assignment_objectives per class per request. Later callers
    reuse the set from the request cache.
    """
    cache = _request_cache()
    key = ("_attached_objective_ids", str(class_id))
    if key in cache:
        return cache[key]
    rows = fetch_all_rows(
        supabase_admin.table("assignment_objectives")
        .select(
            "assignment_id, learning_objective_id, "
            "assignments!assignment_objectives_assignment_id_fkey!inner(class_id)"
        )
        .eq("assignments.class_id", str(class_id))
        .order("assignment_id")
        .order("learning_objective_id")
    )
    ids = {
        str(row.get("learning_objective_id"))
        for row in rows
        if row.get("learning_objective_id")
    }
    cache[key] = ids
    return ids


def objectives_attached_to_assignments(
    class_id: str, pool: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Pool rows linked to at least one assignment. Each objective is kept once."""
    rows = list(pool or [])
    if not class_id or not rows:
        return rows
    attached_ids = _attached_objective_ids(str(class_id))
    return [lo for lo in rows if str(lo.get("id") or "") in attached_ids]


def class_progress(raw_grades, lo_lookup):
    """Return (aggregated rows, class objective total) for one class's objective lookup."""
    if not lo_lookup:
        return [], 0
    return _aggregate_lo_grades(raw_grades or [], lo_lookup), len(lo_lookup)


def _process_enrollments(class_data):
    """Extract students from enrollment data, aggregating grades per LO.

    Returns:
        (active_students, all_students, lo_lookup)
        - active_students: non-muted students with aggregated grades
        - all_students: all students (including muted) with aggregated grades
        - lo_lookup: {str(lo_id): lo_dict}
    """
    pool = class_data.get("learning_objectives") or []
    if class_data.get("id"):
        pool = objectives_attached_to_assignments(class_data.get("id"), pool)
    lo_lookup = class_lo_lookup(pool)
    active_students = []
    all_students = []
    for e in class_data.get("enrollments", []):
        prof = normalize_profile(e)
        if not prof or not prof.get("id"):
            continue
        prof["learning_objectives"], prof["objective_total"] = class_progress(
            prof.get("grades"), lo_lookup
        )
        prof['name'] = display_name(prof.get("full_name"))
        prof['email'] = (prof.get('email') or '').strip()
        prof['muted'] = e.get('muted', False)
        all_students.append(prof)
        if not prof['muted']:
            active_students.append(prof)
    active_students.sort(key=row_sort_key)
    all_students.sort(key=row_sort_key)
    return active_students, all_students, lo_lookup
