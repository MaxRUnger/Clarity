from typing import Any, Dict, List

from flask import jsonify

from app.helpers.authentication import supabase_admin
from app.helpers.paging import fetch_all_rows
from app.helpers.student_names import (
    COMMA_REQUIRED,
    name_key,
    normalize_email,
    require_canvas_name,
)


def _instructor_student_email_index(owner_id: str) -> Dict[str, Dict[str, Any]]:
    """Map a lowercased email to the student enrolled in this instructor's classes.

    The first profile seen for an email is kept. Later rows for that same
    profile add class ids. A different profile with the same email is ignored.
    """
    class_rows = fetch_all_rows(
        supabase_admin.table("classes").select("id").eq("instructor_id", owner_id).order("id")
    )
    class_ids = [str(row.get("id")) for row in class_rows if row.get("id")]
    index: Dict[str, Dict[str, Any]] = {}
    if not class_ids:
        return index
    enroll_rows = fetch_all_rows(
        supabase_admin.table("enrollments")
        .select("class_id, student_id, profiles(id, full_name, email)")
        .in_("class_id", class_ids)
        .order("id")
    )
    for row in enroll_rows:
        prof = row.get("profiles") or {}
        if isinstance(prof, list):
            prof = prof[0] if prof else {}
        email = normalize_email(prof.get("email") or "")
        if not email:
            continue
        profile_id = str(prof.get("id") or row.get("student_id") or "").strip()
        if not profile_id:
            continue
        enrolled_class = str(row.get("class_id") or "").strip()
        entry = index.get(email)
        if entry is None:
            index[email] = {
                "profile_id": profile_id,
                "full_name": prof.get("full_name") or "",
                "class_ids": {enrolled_class} if enrolled_class else set(),
            }
            continue
        if entry["profile_id"] != profile_id or not enrolled_class:
            continue
        entry["class_ids"].add(enrolled_class)
    return index


def decide_student_enrollment(index, class_id, email, typed_name):
    """Return outcome, stored name, and profile id for one normalized email.

    outcome is already_in_class, name_differs, reuse, or create.
    stored_name and profile_id are empty when the outcome is create.
    """
    entry = (index or {}).get(email)
    if not entry:
        return "create", "", ""
    stored_name = entry.get("full_name") or ""
    profile_id = str(entry.get("profile_id") or "")
    if str(class_id) in (entry.get("class_ids") or ()):
        return "already_in_class", stored_name, profile_id
    if name_key(typed_name) != name_key(stored_name):
        return "name_differs", stored_name, profile_id
    return "reuse", stored_name, profile_id


def validate_student_name_and_email(raw_name, raw_email):
    """Return (canvas name, normalized email, error). One error string for add and edit."""
    if not str(raw_email).strip() or not str(raw_name).strip():
        return None, None, "Email and name are required"
    name = require_canvas_name(raw_name)
    if not name:
        return None, None, COMMA_REQUIRED
    if len(name) > 255:
        return None, None, "Name must be 255 characters or fewer"
    if len(str(raw_email).strip()) > 255:
        return None, None, "Email must be 255 characters or fewer"
    email = normalize_email(str(raw_email))
    if not email:
        return None, None, "Invalid email format"
    return name, email, None


def _profile_used_by_another_instructor(student_id, owner_id) -> bool:
    rows = fetch_all_rows(
        supabase_admin.table("enrollments")
        .select("class_id, classes(instructor_id)")
        .eq("student_id", student_id)
        .order("id")
    )
    owner = str(owner_id or "")
    for row in rows:
        classes = row.get("classes") or {}
        if isinstance(classes, list):
            classes = classes[0] if classes else {}
        if not isinstance(classes, dict):
            return True
        instructor_id = str(classes.get("instructor_id") or "")
        if instructor_id != owner:
            return True
    return False


def _student_csv_rejection(errors: List[str]):
    shown = errors[:25]
    summary = "\n".join(shown)
    extra = len(errors) - len(shown)
    if extra:
        summary += f"\nand {extra} more rows have errors."
    return jsonify({
        "success": False,
        "error": summary,
        "errors": errors,
    }), 400


def _classify_student_csv_rows(class_id, owner_id, rows, parse_errors):
    """Return row errors and actions. A non-empty error list means write nothing."""
    errors = list(parse_errors)
    if not rows:
        return errors, []
    index = _instructor_student_email_index(owner_id)
    seen = set()
    actions = []
    for row in sorted(rows, key=lambda item: int(item["line"])):
        line = row["line"]
        email = row["email"]
        name = row["full_name"]
        if email in seen:
            errors.append(f"Row {line}: {email} is listed more than once.")
            continue
        seen.add(email)
        outcome, stored_name, profile_id = decide_student_enrollment(
            index, class_id, email, name
        )
        if outcome == "already_in_class":
            errors.append(f"Row {line}: {email} is already in this class.")
        elif outcome == "name_differs":
            errors.append(
                f"Row {line}: {email} belongs to {stored_name}. "
                f"Enter the name as {stored_name}."
            )
        elif outcome == "reuse":
            actions.append({
                "outcome": "reuse",
                "full_name": stored_name,
                "email": email,
                "profile_id": profile_id,
            })
        else:
            actions.append({
                "outcome": "create",
                "full_name": name,
                "email": email,
                "profile_id": "",
            })
    if errors:
        return errors, []
    return [], actions
