import logging
import time
from datetime import timedelta

from app.helpers.authentication import supabase_admin
from app.helpers.class_data import _class_data_for_progress
from app.helpers.class_form import class_display_title
from app.helpers.class_progress import _process_enrollments
from app.helpers.report_email import (
    EMAIL_MAX_RECIPIENTS,
    _ADDRESS_KINDS,
    _cap_block_reason,
    _email_cap_counts,
    _record_email_send,
    _saved_address_problem,
    _send_via_resend,
    email_recipient_hash,
)
from app.helpers.revision_report import _revision_view
from app.helpers.revision_text import _revision_message
from app.helpers.student_names import display_name
from app.models import Course, Grade

logger = logging.getLogger(__name__)

_REVISION_EMAIL_BUDGET_SECONDS = 60
_REVISION_EMAIL_PAUSE_SECONDS = 0.5


def _revision_named_rows(class_id, today):
    class_data = Course.get_full_class_data(class_id)
    if not class_data:
        return [], []
    progress = _class_data_for_progress(class_data)
    active, _, _ = _process_enrollments(progress)
    names = {
        student["id"]: display_name(student.get("full_name") or "")
        for student in active
    }
    named = []
    for row in Grade.get_revision_candidates(class_id, today):
        if row.get("student_id") in names:
            row["student_name"] = names[row["student_id"]]
            named.append(row)
    return named, active


def _load_sent_reminder_keys(class_id: str, due_iso: str):
    resp = (
        supabase_admin.table("revision_reminders_sent")
        .select("student_id, assignment_id, learning_objective_id")
        .eq("class_id", class_id)
        .eq("revision_due", due_iso)
        .execute()
    )
    keys = set()
    for row in resp.data or []:
        keys.add((
            str(row.get("student_id") or ""),
            str(row.get("assignment_id") or ""),
            str(row.get("learning_objective_id") or ""),
        ))
    return keys


def revision_reminder_partition(class_id, today, named_rows=None, students=None):
    """Recipients, students with no usable email, and students already reminded.

    The reminders table is read once. Pass named_rows and students from a page
    that already loaded them. Preview and send omit both and load them here.
    """
    due_iso = (today + timedelta(days=3)).isoformat()
    if named_rows is None or students is None:
        named_rows, students = _revision_named_rows(class_id, today)
    email_by_id = {}
    set_at_by_id = {}
    for student in students or []:
        sid = str(student.get("id") or "")
        email_by_id[sid] = str(student.get("email") or "").strip()
        set_at_by_id[sid] = student.get("email_set_at")
    sent = _load_sent_reminder_keys(class_id, due_iso)
    due_rows = [
        row for row in (named_rows or [])
        if (row.get("revision_due") or "") == due_iso
    ]
    open_rows = []
    for row in due_rows:
        key = (
            str(row.get("student_id") or ""),
            str(row.get("assignment_id") or ""),
            str(row.get("learning_objective_id") or ""),
        )
        if key not in sent:
            open_rows.append(row)
    everyone = _revision_view("3", "", "", "", due_rows)["students"]
    still_open = {
        str(student["student_id"]): student
        for student in _revision_view("3", "", "", "", open_rows)["students"]
    }
    recipients = []
    no_email = []
    invalid = []
    held = []
    already_reminded = []
    for student in everyone:
        sid = str(student["student_id"])
        if sid not in still_open:
            already_reminded.append({
                "student_id": sid,
                "name": student["student_name"],
            })
            continue
        email, kind = _saved_address_problem(
            email_by_id.get(sid, ""),
            set_at_by_id.get(sid),
        )
        if kind == "invalid":
            invalid.append({
                "student_id": sid,
                "name": student["student_name"],
                "message": _ADDRESS_KINDS["invalid"][0],
            })
            continue
        if kind == "missing":
            no_email.append({
                "student_id": sid,
                "name": student["student_name"],
            })
            continue
        if kind == "held":
            held.append({
                "student_id": sid,
                "name": student["student_name"],
                "message": _ADDRESS_KINDS["held"][0],
            })
            continue
        recipients.append({
            "student_id": sid,
            "name": student["student_name"],
            "email": email,
            "assignments": still_open[sid]["assignments"],
        })
    return {
        "due": due_iso,
        "recipients": recipients,
        "no_email": no_email,
        "invalid": invalid,
        "held": held,
        "already_reminded": already_reminded,
    }


def _attach_revision_messages(partition, class_title, signature):
    subject = f"Revision due in 3 days: {class_title}"
    for student in partition["recipients"]:
        student["subject"] = subject
        student["body"] = _revision_message(
            student, partition["due"], class_title, signature
        )
    return subject


def _insert_reminder_rows(class_id, student, due_iso, sent_by):
    rows = []
    for assignment in student.get("assignments") or []:
        for obj in assignment.get("objectives") or []:
            rows.append({
                "class_id": class_id,
                "student_id": student["student_id"],
                "assignment_id": assignment["assignment_id"],
                "learning_objective_id": obj["learning_objective_id"],
                "revision_due": due_iso,
                "sent_by": sent_by,
            })
    if rows:
        supabase_admin.table("revision_reminders_sent").upsert(
            rows,
            on_conflict="student_id,assignment_id,learning_objective_id,revision_due",
            ignore_duplicates=True,
        ).execute()


def _send_revision_reminders(class_id, recipients, due_iso, sender_name, sent_by):
    hashes = [
        email_recipient_hash(student["email"])
        for student in recipients
        if student.get("email")
    ]
    instructor_count = 0
    per_hash = {}
    if hashes:
        loaded = _email_cap_counts(sent_by, hashes)
        if loaded is None:
            return None
        instructor_count, per_hash = loaded
    deadline = time.monotonic() + _REVISION_EMAIL_BUDGET_SECONDS
    results = []
    attempted = 0
    sent_now = 0
    hash_sent = {}
    for index, student in enumerate(recipients):
        if index >= EMAIL_MAX_RECIPIENTS or time.monotonic() >= deadline:
            break
        if index:
            time.sleep(_REVISION_EMAIL_PAUSE_SECONDS)
            if time.monotonic() >= deadline:
                break
        digest = email_recipient_hash(student["email"])
        reason = _cap_block_reason(
            instructor_count + sent_now,
            per_hash.get(digest, 0) + hash_sent.get(digest, 0),
        )
        if reason:
            results.append({
                "student_id": student["student_id"],
                "name": student["name"],
                "status": "capped",
                "message": reason,
            })
            attempted = index + 1
            continue
        ok, _err = _send_via_resend(
            student["email"],
            student["subject"],
            student["body"],
            sender_name=sender_name,
        )
        if not ok:
            results.append({
                "student_id": student["student_id"],
                "name": student["name"],
                "status": "failed",
            })
            attempted = index + 1
            continue
        _record_email_send(
            sent_by, class_id, student["student_id"], digest, "revision"
        )
        sent_now += 1
        hash_sent[digest] = hash_sent.get(digest, 0) + 1
        try:
            _insert_reminder_rows(class_id, student, due_iso, sent_by)
        except Exception:
            logger.error(
                "Revision reminder accepted but not recorded for class %s student %s",
                class_id,
                student["student_id"],
            )
            results.append({
                "student_id": student["student_id"],
                "name": student["name"],
                "status": "sent_not_recorded",
            })
            attempted = index + 1
            continue
        results.append({
            "student_id": student["student_id"],
            "name": student["name"],
            "status": "sent",
        })
        attempted = index + 1
    for student in recipients[attempted:]:
        results.append({
            "student_id": student["student_id"],
            "name": student["name"],
            "status": "not_attempted",
        })
    return results


def _reminder_counts(results):
    counts = {
        "sent": 0,
        "failed": 0,
        "skipped": 0,
        "not_attempted": 0,
        "sent_not_recorded": 0,
        "held": 0,
        "invalid": 0,
        "capped": 0,
    }
    for row in results:
        status = row["status"]
        if status in counts:
            counts[status] += 1
    return counts


def _revision_class_title(class_id):
    resp = (
        supabase_admin.table("classes")
        .select("name, days, section_number")
        .eq("id", class_id)
        .limit(1)
        .execute()
    )
    row = {}
    if isinstance(resp.data, list) and resp.data and isinstance(resp.data[0], dict):
        row = resp.data[0]
    return class_display_title(
        row.get("name"),
        row.get("days"),
        row.get("section_number"),
    )


def _public_reminder_student(student):
    item = {
        "student_id": student["student_id"],
        "name": student["name"],
    }
    if student.get("email"):
        item["email"] = student["email"]
    if student.get("message"):
        item["message"] = student["message"]
    return item
