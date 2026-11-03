import csv
import io
import re
from typing import Any, Dict, List, Optional, Tuple

from app.helpers.student_names import (
    COMMA_REQUIRED,
    normalize_email,
    raw_name_from_csv_fields,
    require_canvas_name,
    row_sort_key,
)
from app.models import DEFAULT_REQUIRED_MS, Homework


MAX_IMPORT_ROWS = 2000

def _normalize_csv_key(key: Any) -> str:
    nk = str(key or "").strip().lower().lstrip("\ufeff")
    return re.sub(r"[^a-z0-9]+", "_", nk).strip("_")


def _csv_row_norm_keys(row: Dict[str, Any]) -> Dict[str, str]:
    """Lowercase + strip CSV header keys (handles UTF-8 BOM on first column)."""
    out: Dict[str, str] = {}
    for k, v in row.items():
        if k is None:
            continue
        nk = _normalize_csv_key(k)
        if isinstance(v, str):
            out[nk] = v.strip()
        elif v is None:
            out[nk] = ""
        else:
            out[nk] = str(v).strip()
    return out

def parse_learning_objectives_csv_text(text: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Parse CSV for Canvas-style outcomes: required title; optional description and calculation_int.

    Initial required_ms is derived from calculation_int when valid (clamped 1–5); otherwise defaults.
    Instructors adjust Ms on the confirm-import UI, so calculation_int issues are handled silently.
    """
    warnings: List[str] = []
    text = (text or "").strip()
    if not text:
        return [], ["File is empty"]
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["Missing header row"]
    need = {"title"}
    lower_fn = {_normalize_csv_key(f) for f in reader.fieldnames if f}
    missing = need - lower_fn
    if missing:
        return [], [
            "CSV must include a title column (Canvas outcomes export). "
            f"Missing: {', '.join(sorted(missing))}"
        ]

    rows_out: List[Dict[str, Any]] = []
    for raw in reader:
        r = _csv_row_norm_keys(raw)
        title = (r.get("title") or "").strip()
        if not title:
            continue
        desc = (r.get("description") or "").strip()
        calc_raw = (r.get("calculation_int") or "").strip()
        required_ms = DEFAULT_REQUIRED_MS
        if calc_raw:
            try:
                ci = int(float(calc_raw))
                required_ms = max(1, min(5, ci))
            except (ValueError, TypeError):
                required_ms = DEFAULT_REQUIRED_MS
        rows_out.append(
            {
                "vendor_code": title,
                "required_ms": required_ms,
                "description": desc or None,
            }
        )
    if not rows_out:
        return [], ["No data rows with a non-empty title"]
    return rows_out, warnings

def parse_students_csv_text(text: str) -> Tuple[List[Dict[str, str]], List[str]]:
    warnings: List[str] = []
    text = (text or "").strip()
    if not text:
        return [], ["File is empty"]

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["Missing header row"]

    lower_fn = {_normalize_csv_key(f) for f in reader.fieldnames if f}
    has_name_col = any(h in lower_fn for h in ("full_name", "student_name", "name", "student"))
    has_first_last = ("first_name" in lower_fn and "last_name" in lower_fn)
    if not has_name_col and not has_first_last:
        return [], [
            "CSV must include either a name/full_name/student_name column, "
            "or first_name and last_name columns."
        ]

    rows: List[Dict[str, str]] = []
    for line_no, raw in enumerate(reader, start=2):
        extras = raw.get(None)
        if extras and any((value or "").strip() for value in extras):
            warnings.append(
                f'Row {line_no}: Put the name in quotes, like "Doe, Jane".'
            )
            continue
        row = _csv_row_norm_keys(raw)
        if not any(row.values()):
            continue
        raw_name = (
            row.get("full_name")
            or row.get("student_name")
            or row.get("name")
            or row.get("student")
            or ""
        )
        first = row.get("first_name") or row.get("first") or ""
        last = row.get("last_name") or row.get("last") or ""
        if not str(raw_name).strip() and (str(first).strip() or str(last).strip()):
            full_name = require_canvas_name(raw_name_from_csv_fields(first, last))
        else:
            full_name = require_canvas_name(raw_name)
        raw_email = (row.get("email") or row.get("student_email") or "").strip()
        email = normalize_email(raw_email) if raw_email else None
        if not full_name:
            warnings.append(f"Row {line_no}: {COMMA_REQUIRED}")
        if not raw_email:
            warnings.append(f"Row {line_no}: Email is required.")
        elif not email:
            warnings.append(f"Row {line_no}: Invalid email format.")
        if not full_name or not email:
            continue
        rows.append({
            "full_name": full_name,
            "email": email,
            "line": str(line_no),
        })

    if not rows and not warnings:
        return [], ["File is empty"]

    rows.sort(key=row_sort_key)
    return rows, warnings

def _strip_csv_formula_prefix(value: str) -> str:
    s = str(value or "").strip()
    if len(s) >= 2 and s[0] == "'" and s[1] in ("=", "+", "-", "@"):
        return s[1:]
    return s


def _csv_row_cells(row) -> List[str]:
    return [_strip_csv_formula_prefix(c) for c in (row or [])]


def _csv_row_is_blank(cells: List[str]) -> bool:
    return all(not c for c in cells)


_GRADESHEET_HEADER_ERROR = (
    "The header row must start with Student Name,HW followed by learning-objective codes."
)

_NOT_GRADESHEET_EXPORT = (
    "This file is not a Clarity gradesheet export. "
    "Download a blank gradesheet for an assignment and fill that in."
)


def _collapse_assignment_title(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _normalize_assignment_title(value) -> str:
    return _collapse_assignment_title(value).casefold()


def parse_blank_gradesheet_csv_text(
    text: str,
    class_vendor_codes: List[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Parse the Download Blank CSV gradesheet. Returns (payload, error).

    Every LO header is present on each student, with "" for an empty cell.
    Import treats "" as clear-if-a-row-exists, and a missing key as skip.
    """
    raw = (text or "").lstrip("\ufeff")
    if not raw.strip():
        return None, "CSV file is empty."

    rows = list(csv.reader(io.StringIO(raw)))
    if len(rows) < 4:
        return None, _NOT_GRADESHEET_EXPORT

    r0 = _csv_row_cells(rows[0])
    r1 = _csv_row_cells(rows[1])
    assignment_name = _collapse_assignment_title(r0[1] if len(r0) > 1 else "")
    if (
        not r0
        or r0[0].lower() != "assignment"
        or not _normalize_assignment_title(assignment_name)
    ):
        return None, _NOT_GRADESHEET_EXPORT
    if not r1 or r1[0].lower() != "date":
        return None, _NOT_GRADESHEET_EXPORT

    # Metadata (Assignment, Date, NOTE, extra note lines) may grow. Do not
    # assume a fixed header index. Skip every non-blank row after Date until
    # the blank separator; the following row is the column header.
    sep_idx = 2
    while sep_idx < len(rows) and not _csv_row_is_blank(_csv_row_cells(rows[sep_idx])):
        sep_idx += 1
    if sep_idx >= len(rows):
        return None, "CSV is missing the blank separator row before the header."
    header_idx = sep_idx + 1
    if header_idx >= len(rows):
        return None, "CSV is missing the Student Name,HW header row."

    header = _csv_row_cells(rows[header_idx])
    # Field count before trailing empty cells are removed. The quote check uses this width.
    header_width = len(header)
    while header and not header[-1]:
        header.pop()
    if (
        len(header) < 2
        or header[0].lower() != "student name"
    ):
        return None, _GRADESHEET_HEADER_ERROR

    hw_indexes = [
        idx for idx, col in enumerate(header)
        if idx > 0 and col and col.strip().lower() == "hw"
    ]
    if len(hw_indexes) != 1:
        return None, _GRADESHEET_HEADER_ERROR
    hw_idx = hw_indexes[0]

    vendor_by_lower = {}
    for code in class_vendor_codes:
        c = str(code or "").strip()
        if c:
            vendor_by_lower.setdefault(c.lower(), c)

    lo_headers: List[str] = []
    lo_indexes: List[int] = []
    seen_lo = set()
    unknown: List[str] = []
    for idx, col in enumerate(header):
        if idx == 0 or idx == hw_idx or not col:
            continue
        if Homework.is_hw_prev_header(col):
            continue
        canonical = vendor_by_lower.get(col.lower())
        if not canonical:
            unknown.append(col)
            continue
        key = canonical.lower()
        if key in seen_lo:
            return None, f'Duplicate learning-objective column "{canonical}".'
        seen_lo.add(key)
        lo_headers.append(canonical)
        lo_indexes.append(idx)
    if unknown:
        shown = ", ".join(unknown[:8])
        extra = f" (and {len(unknown) - 8} more)" if len(unknown) > 8 else ""
        return None, (
            "CSV learning-objective headers must match this class's vendor codes. "
            f"Unknown: {shown}{extra}."
        )

    students: List[Dict[str, Any]] = []
    for offset, raw_row in enumerate(rows[header_idx + 1:]):
        file_row = header_idx + offset + 2
        cells = _csv_row_cells(raw_row)
        if _csv_row_is_blank(cells):
            continue
        if len(raw_row) > header_width:
            return None, (
                f"Row {file_row}: put the student name in quotes because it contains a comma"
            )
        name = cells[0] if cells else ""
        if not name:
            continue
        hw_raw = cells[hw_idx] if hw_idx < len(cells) else ""
        homework_pct = None
        if hw_raw:
            parsed_hw = Homework.parse_import_hw_pct(hw_raw)
            homework_pct = str(parsed_hw) if parsed_hw is not None else hw_raw

        grades: Dict[str, str] = {}
        for lo_code, idx in zip(lo_headers, lo_indexes):
            mark = cells[idx] if idx < len(cells) else ""
            grades[lo_code] = mark.upper() if mark else ""

        students.append({
            "name": name,
            "grades": grades,
            "homework_pct": homework_pct,
        })
        if len(students) > MAX_IMPORT_ROWS:
            return None, f"CSV has too many student rows (max {MAX_IMPORT_ROWS})."

    return {
        "assignment_name": assignment_name,
        "date_value": r1[1] if len(r1) > 1 else "",
        "learning_objectives": lo_headers,
        "homework_column": "HW",
        "students": students,
        "extraction_path": "csv",
    }, None
