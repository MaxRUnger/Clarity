"""Gzip JSON snapshot of one class. The downloadable zip is unchanged."""

import gzip
import io
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Sequence

from app.authentication import supabase_admin
from app.paging import fetch_all_rows

FORMAT_VERSION = 1
# Each .in_() list holds at most this many ids.
_IN_CHUNK_SIZE = 50
_ID_CHUNK = 100
_SCOPED_TABLES = (
    "learning_objectives",
    "assignments",
    "enrollments",
    "homework_scores",
    "free_passes",
)


class SnapshotCountError(Exception):
    """A snapshot count does not match the rows that were read or the zip manifest."""


MAX_GZIP_BYTES = 50_000_000


class GzipTooLarge(Exception):
    """The decompressed snapshot is larger than MAX_GZIP_BYTES."""


def gunzip_bounded(blob: bytes) -> bytes:
    """Inflate a snapshot and stop once the output passes MAX_GZIP_BYTES."""
    chunks = []
    total = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(blob)) as handle:
            while True:
                block = handle.read(65536)
                if not block:
                    break
                total += len(block)
                if total > MAX_GZIP_BYTES:
                    raise GzipTooLarge()
                chunks.append(block)
    except GzipTooLarge:
        raise
    except Exception as exc:
        raise SnapshotCountError("gzip") from exc
    return b"".join(chunks)


def _json_default(value: Any) -> str:
    return str(value)


def _exact_count(query) -> int:
    response = query.execute()
    if response.count is None:
        raise SnapshotCountError("missing count")
    return int(response.count)


def _ordered(query, columns: Sequence[str]):
    for column in columns:
        query = query.order(column)
    return query


def _chunks(values: Sequence[str]) -> List[List[str]]:
    return [list(values[index:index + _ID_CHUNK]) for index in range(0, len(values), _ID_CHUNK)]


def _row_ids(rows: Sequence[Dict[str, Any]]) -> List[str]:
    found: List[str] = []
    seen = set()
    for row in rows:
        value = str(row.get("id") or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        found.append(value)
    return found


def _reject_email_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if "email" in str(key).lower():
                raise SnapshotCountError("email")
            _reject_email_keys(item)
    elif isinstance(value, list):
        for item in value:
            _reject_email_keys(item)


def _read_one_class(class_id: str) -> List[Dict[str, Any]]:
    counted = _exact_count(
        supabase_admin.table("classes")
        .select("id", count="exact", head=True)
        .eq("id", class_id)
    )
    rows = fetch_all_rows(
        supabase_admin.table("classes").select("*").eq("id", class_id).order("id")
    )
    if len(rows) != counted or len(rows) != 1:
        raise SnapshotCountError("classes")
    return rows


def _read_class_scoped(table: str, class_id: str) -> List[Dict[str, Any]]:
    counted = _exact_count(
        supabase_admin.table(table)
        .select("id", count="exact", head=True)
        .eq("class_id", class_id)
    )
    rows = fetch_all_rows(
        supabase_admin.table(table).select("*").eq("class_id", class_id).order("id")
    )
    if len(rows) != counted:
        raise SnapshotCountError(table)
    return rows


def _read_in(
    table: str,
    column: str,
    values: Sequence[str],
    order: Sequence[str],
    count_column: str,
) -> List[Dict[str, Any]]:
    counted = _exact_count(
        supabase_admin.table(table)
        .select(count_column, count="exact", head=True)
        .in_(column, list(values))
    )
    rows = fetch_all_rows(
        _ordered(
            supabase_admin.table(table).select("*").in_(column, list(values)),
            order,
        )
    )
    if len(rows) != counted:
        raise SnapshotCountError(table)
    return rows


def _row_key(row: Dict[str, Any]):
    if row.get("id"):
        return ("id", str(row.get("id")))
    return (
        "pair",
        str(row.get("assignment_id") or ""),
        str(row.get("learning_objective_id") or ""),
    )


def _read_linked(
    table: str,
    assignment_ids: Sequence[str],
    objective_ids: Sequence[str],
    order: Sequence[str],
    count_column: str,
) -> List[Dict[str, Any]]:
    if not assignment_ids and not objective_ids:
        return []
    found: List[Dict[str, Any]] = []
    seen = set()
    for column, values in (
        ("assignment_id", assignment_ids),
        ("learning_objective_id", objective_ids),
    ):
        for start in range(0, len(values), _IN_CHUNK_SIZE):
            group = list(values[start:start + _IN_CHUNK_SIZE])
            for row in _read_in(table, column, group, order, count_column):
                key = _row_key(row)
                if key in seen:
                    continue
                seen.add(key)
                found.append(row)
    return found


def _read_students(enrollments: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ids: List[str] = []
    seen = set()
    for row in enrollments:
        student_id = str(row.get("student_id") or "").strip()
        if not student_id or student_id in seen:
            continue
        seen.add(student_id)
        ids.append(student_id)
    found: List[Dict[str, Any]] = []
    for group in _chunks(ids):
        counted = _exact_count(
            supabase_admin.table("profiles")
            .select("id", count="exact", head=True)
            .in_("id", group)
        )
        rows = fetch_all_rows(
            supabase_admin.table("profiles")
            .select("id, full_name")
            .in_("id", group)
            .order("id")
        )
        if len(rows) != counted:
            raise SnapshotCountError("students")
        found.extend(rows)
    if len(found) != len(ids):
        raise SnapshotCountError("students")
    students = []
    for row in sorted(found, key=lambda item: str(item.get("id") or "")):
        students.append({
            "id": row.get("id"),
            "full_name": row.get("full_name"),
        })
    return students


def _distinct_student_ids(enrollments: Sequence[Any]) -> set:
    found = set()
    for row in enrollments:
        if not isinstance(row, dict):
            continue
        student_id = str(row.get("student_id") or "").strip()
        if student_id:
            found.add(student_id)
    return found


def check_snapshot_archive(snapshot_bytes: bytes, manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Gunzip and require version 1, the manifest enrollment count, and distinct students."""
    raw = gunzip_bounded(snapshot_bytes)
    try:
        document = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise SnapshotCountError("json") from exc
    if not isinstance(document, dict):
        raise SnapshotCountError("document")
    if document.get("format_version") != FORMAT_VERSION:
        raise SnapshotCountError("format_version")
    archived_at = document.get("archived_at")
    if not isinstance(archived_at, str) or not archived_at.strip():
        raise SnapshotCountError("archived_at")
    for name in ("grade_change_log", "revision_reminders_sent"):
        if name in document:
            raise SnapshotCountError(name)
    _reject_email_keys(document)
    classes = document.get("classes")
    assignments = document.get("assignments")
    enrollments = document.get("enrollments")
    students = document.get("students")
    if not isinstance(classes, list) or len(classes) != 1:
        raise SnapshotCountError("classes")
    if not isinstance(assignments, list) or len(assignments) != manifest.get("source_assignments"):
        raise SnapshotCountError("assignments")
    if not isinstance(enrollments, list) or len(enrollments) != manifest.get("source_students"):
        raise SnapshotCountError("enrollments")
    if not isinstance(students, list) or len(students) != len(_distinct_student_ids(enrollments)):
        raise SnapshotCountError("students")
    for key in (
        "learning_objectives",
        "assignment_objectives",
        "grades",
        "homework_scores",
        "free_passes",
    ):
        if not isinstance(document.get(key), list):
            raise SnapshotCountError(key)
    for student in students:
        if not isinstance(student, dict) or set(student.keys()) != {"id", "full_name"}:
            raise SnapshotCountError("students")
    return document


def build_class_snapshot(class_id: str, manifest: Dict[str, Any]) -> bytes:
    """Complete gzip snapshot for this class. Raises when a count disagrees."""
    class_id = str(class_id)
    classes = _read_one_class(class_id)
    scoped = {
        table: _read_class_scoped(table, class_id)
        for table in _SCOPED_TABLES
    }
    assignment_ids = _row_ids(scoped["assignments"])
    objective_ids = _row_ids(scoped["learning_objectives"])
    assignment_objectives = _read_linked(
        "assignment_objectives",
        assignment_ids,
        objective_ids,
        ("assignment_id", "learning_objective_id"),
        "assignment_id",
    )
    grades = _read_linked(
        "grades",
        assignment_ids,
        objective_ids,
        ("id",),
        "id",
    )
    students = _read_students(scoped["enrollments"])
    document = {
        "format_version": FORMAT_VERSION,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "classes": classes,
        "learning_objectives": scoped["learning_objectives"],
        "assignments": scoped["assignments"],
        "assignment_objectives": assignment_objectives,
        "enrollments": scoped["enrollments"],
        "grades": grades,
        "homework_scores": scoped["homework_scores"],
        "free_passes": scoped["free_passes"],
        "students": students,
    }
    read_counts = {
        "classes": len(classes),
        "learning_objectives": len(scoped["learning_objectives"]),
        "assignments": len(scoped["assignments"]),
        "assignment_objectives": len(assignment_objectives),
        "enrollments": len(scoped["enrollments"]),
        "grades": len(grades),
        "homework_scores": len(scoped["homework_scores"]),
        "free_passes": len(scoped["free_passes"]),
        "students": len(students),
    }
    encoded = json.dumps(
        document, ensure_ascii=False, sort_keys=True, default=_json_default
    ).encode("utf-8")
    del document, classes, scoped, assignment_objectives, grades, students
    payload = gzip.compress(encoded)
    del encoded
    checked = check_snapshot_archive(payload, manifest)
    for key, count in read_counts.items():
        if len(checked[key]) != count:
            raise SnapshotCountError(key)
    return payload
