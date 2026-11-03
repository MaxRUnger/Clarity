from datetime import timedelta

from app.helpers.lo_order import lo_sort_key
from app.helpers.student_names import collator_key

def _sort_overdue_revisions(revisions):
    """Order overdue rows by the stored student name. Equal names keep their order."""
    revisions.sort(key=lambda rev: collator_key(rev.get("student_name") or ""))
    return revisions


def _revision_count_line(student_count, revision_count):
    students = "student" if student_count == 1 else "students"
    revisions = "revision" if revision_count == 1 else "revisions"
    return f"{student_count} {students}, {revision_count} {revisions}"


def _objective_shows_name(code, name):
    text = (name or "").strip()
    if not text:
        return False
    return text.casefold() != (code or "").strip().casefold()


def _revision_view(key, label, print_title, empty_message, rows):
    grouped = {}
    order = []
    for row in rows:
        sid = row.get("student_id")
        if sid not in grouped:
            grouped[sid] = {
                "student_id": sid,
                "student_name": row.get("student_name") or "",
                "assignments": {},
                "assignment_order": [],
            }
            order.append(sid)
        student = grouped[sid]
        aid = row.get("assignment_id") or row.get("assignment_name") or ""
        if aid not in student["assignments"]:
            student["assignments"][aid] = {
                "assignment_id": aid,
                "assignment_name": row.get("assignment_name") or "",
                "revision_due": row.get("revision_due") or "",
                "objectives": [],
            }
            student["assignment_order"].append(aid)
        code = row.get("lo_code") or ""
        name = row.get("lo_objective_name") or ""
        student["assignments"][aid]["objectives"].append({
            "lo_code": code,
            "lo_name": name,
            "show_name": _objective_shows_name(code, name),
            "top_score": row.get("top_score") or "",
            "learning_objective_id": row.get("learning_objective_id") or "",
        })
    students = []
    revision_count = 0
    for sid in order:
        student = grouped[sid]
        assignments = [student["assignments"][aid] for aid in student["assignment_order"]]
        assignments.sort(key=lambda item: (
            item.get("revision_due") or "",
            collator_key(item.get("assignment_name") or ""),
            item.get("assignment_id") or "",
        ))
        for assignment in assignments:
            assignment["objectives"].sort(key=lambda obj: lo_sort_key({
                "vendor_code": obj.get("lo_code") or "",
                "name": obj.get("lo_name") or "",
                "id": obj.get("learning_objective_id") or "",
            }))
            revision_count += len(assignment["objectives"])
        students.append({
            "student_id": student["student_id"],
            "student_name": student["student_name"],
            "assignments": assignments,
        })
    _sort_overdue_revisions(students)
    return {
        "key": key,
        "label": label,
        "print_title": print_title,
        "empty_message": empty_message,
        "students": students,
        "student_count": len(students),
        "revision_count": revision_count,
        "count_line": _revision_count_line(len(students), revision_count),
    }


def build_revision_report(rows, today):
    """Group named revision rows into Overdue, Due today, and exact due-in-1..7 views."""
    today_iso = today.isoformat()
    buckets = {"overdue": [], "today": []}
    for n in range(1, 8):
        buckets[str(n)] = []
    day_dates = {n: (today + timedelta(days=n)).isoformat() for n in range(1, 8)}
    for row in rows:
        due = row.get("revision_due") or ""
        if due and due < today_iso:
            buckets["overdue"].append(row)
        elif due == today_iso:
            buckets["today"].append(row)
        else:
            for n, iso in day_dates.items():
                if due == iso:
                    buckets[str(n)].append(row)
                    break
    views = [
        _revision_view(
            "overdue",
            "Overdue",
            "Overdue revisions",
            "No overdue revisions.",
            buckets["overdue"],
        ),
        _revision_view(
            "today",
            "Due today",
            "Revisions due today",
            "No revisions due today",
            buckets["today"],
        ),
    ]
    for n in range(1, 8):
        noun = "day" if n == 1 else "days"
        star = " ★" if n == 3 else ""
        views.append(_revision_view(
            str(n),
            f"Due in {n} {noun}{star}",
            f"Revisions due in {n} {noun}",
            f"No revisions due in {n} {noun}",
            buckets[str(n)],
        ))
    return {"views": views, "overdue_revision_count": views[0]["revision_count"]}
