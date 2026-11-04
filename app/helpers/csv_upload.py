from typing import Any, Dict, List, Optional, Tuple

from flask import request

from app.helpers.csv_text import (
    MAX_IMPORT_ROWS,
    parse_learning_objectives_csv_text,
    parse_students_csv_text,
)


# Shared caps for all bulk-import entry points (CSV and JSON body alike).
# Bytes cap matches the pre-existing LO CSV limit; row cap keeps a single
# request from blocking a worker on thousands of upserts / DoS-ing memory.
MAX_IMPORT_UPLOAD_BYTES = 5 * 1024 * 1024


def _parse_lo_csv_upload() -> Tuple[Optional[str], Optional[List[Dict[str, Any]]], List[str]]:
    """Read CSV from request.files['file']; return (error_message or None, rows or None, warnings)."""
    f = request.files.get("file")
    if not f or not f.filename:
        return "No file uploaded", None, []
    if not f.filename.lower().endswith(".csv"):
        return "Upload a .csv file", None, []
    try:
        raw = f.read()
        if len(raw) > MAX_IMPORT_UPLOAD_BYTES:
            return "CSV must be 5MB or smaller", None, []
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return "CSV must be UTF-8 encoded", None, []
    rows, parse_warnings = parse_learning_objectives_csv_text(text)
    if not rows:
        return parse_warnings[0] if parse_warnings else "No objectives to import", None, parse_warnings
    if len(rows) > MAX_IMPORT_ROWS:
        return f"CSV has too many rows (max {MAX_IMPORT_ROWS})", None, parse_warnings
    return None, rows, parse_warnings


def _parse_student_csv_upload() -> Tuple[Optional[str], Optional[List[Dict[str, str]]], List[str]]:
    """Read CSV from request.files['file']; return (error_message or None, rows or None, row errors)."""
    f = request.files.get("file")
    if not f or not (f.filename or "").strip():
        return "Please choose a CSV file.", None, []
    if not f.filename.lower().endswith(".csv"):
        return "Upload a .csv file", None, []
    try:
        raw = f.read()
        if len(raw) > MAX_IMPORT_UPLOAD_BYTES:
            return "CSV must be 5MB or smaller", None, []
        text = raw.decode("utf-8-sig", errors="replace")
    except Exception:
        return "Could not read CSV file", None, []

    rows, parse_errors = parse_students_csv_text(text)
    if not rows and parse_errors and not str(parse_errors[0]).startswith("Row "):
        return parse_errors[0], None, parse_errors
    if len(rows) > MAX_IMPORT_ROWS:
        return f"CSV has too many rows (max {MAX_IMPORT_ROWS})", None, parse_errors
    return None, rows, parse_errors
