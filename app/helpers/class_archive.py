import logging
import re
from typing import Any, Dict, List

from flask import jsonify

from app.helpers.authentication import supabase_admin

logger = logging.getLogger(__name__)


_ARCHIVE_ID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _owned_archive_row(archive_id, instructor_id):
    """The archive row for this instructor. An id that is not a UUID is missing."""
    if _ARCHIVE_ID.fullmatch(str(archive_id or "")) is None:
        return None
    resp = (
        supabase_admin.table("archived_classes")
        .select("id, class_name, storage_path, snapshot_path, restored_at, instructor_id")
        .eq("id", archive_id)
        .eq("instructor_id", instructor_id)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


def _removed_archive_is_already_gone(exc: BaseException) -> bool:
    from storage3.exceptions import StorageApiError
    return isinstance(exc, StorageApiError) and str(exc.status) == "404"


def _remove_archive_object(path: str) -> None:
    """Remove one stored archive object. A missing object is not an error."""
    if not str(path or "").strip():
        return
    from app.helpers.archive_storage import remove_archive
    try:
        remove_archive(path)
    except Exception as exc:
        if _removed_archive_is_already_gone(exc):
            logger.error("Archive file %s was already gone: %s", path, exc)
            return
        raise


MAX_ARCHIVE_ZIP_BYTES = 50_000_000


def _zip_over_archive_limit(zip_bytes):
    """Return a 413 response when the zip is over the shared archive cap."""
    if len(zip_bytes) <= MAX_ARCHIVE_ZIP_BYTES:
        return None
    megabytes = f"{len(zip_bytes) / 1_000_000:.1f}"
    return jsonify({
        "success": False,
        "error": (
            f"This archive is {megabytes} MB, over the 50 MB limit. "
            "Nothing was changed."
        ),
    }), 413


def _archive_size_label(size_bytes) -> str:
    try:
        size = int(size_bytes)
    except (TypeError, ValueError):
        return ""
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    return f"{max(size, 0) / 1_000:.1f} KB"


_RESTORE_TABLES = (
    "classes",
    "learning_objectives",
    "assignments",
    "assignment_objectives",
    "enrollments",
    "homework_scores",
    "free_passes",
    "students",
    "grades",
)
_RESTORE_ROW_CAP = 400
_RESTORE_BYTE_CAP = 256 * 1024
_RESTORE_PROFILE_PREFIX = "A student in this archive no longer has a profile:"
_RESTORE_EXACT_ERRORS = frozenset({
    "This archive was not found.",
    "This archive is not restorable.",
    "This archive has already been restored.",
    "A class with this name, days, and section already exists. Archive or rename it first.",
    "The saved archive could not be read completely.",
    "The restored class did not match the archive. Nothing was restored.",
})
_RESTORE_FAILED = "Could not restore the class. Nothing was changed."


def _restore_error_text(exc: BaseException) -> str:
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message.strip():
        return message.strip()
    detail = exc.args[0] if exc.args else None
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    if isinstance(detail, dict):
        for key in ("message", "error"):
            value = detail.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _restore_known_message(text: str):
    if text in _RESTORE_EXACT_ERRORS or text.startswith(_RESTORE_PROFILE_PREFIX):
        return text
    return None


def _restore_chunks(records: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    import json
    chunks: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    running = 2
    for record in records:
        encoded = json.dumps(record, ensure_ascii=False).encode("utf-8")
        addition = len(encoded) + 1
        if 2 + addition > _RESTORE_BYTE_CAP:
            raise ValueError("restore staging row exceeds 256 KB")
        if current and (len(current) + 1 > _RESTORE_ROW_CAP or running + addition > _RESTORE_BYTE_CAP):
            chunks.append(current)
            current = [record]
            running = 2 + addition
        else:
            current.append(record)
            running += addition
    if current:
        chunks.append(current)
    return chunks


def _restore_class_id(data: Any):
    import json
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return None
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    class_id = str(data.get("class_id") or "").strip()
    if _ARCHIVE_ID.fullmatch(class_id) is None:
        return None
    return class_id


def _delete_restore_attempt(attempt_id: str) -> None:
    try:
        supabase_admin.table("archive_restore_stage").delete().eq(
            "attempt_id", attempt_id
        ).execute()
    except Exception as exc:
        logger.error("Could not delete restore attempt %s: %s", attempt_id, exc)
