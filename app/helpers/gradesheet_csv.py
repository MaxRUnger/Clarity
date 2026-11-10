import csv
import io
from typing import Any, Dict, List, Optional, Tuple

from app.helpers.csv_safety import csv_safe_row
from app.helpers.student_names import name_key
from app.models import Homework


def _csv_format_hw_score(score) -> str:
    if score is None:
        return ""
    if score == -1 or score == -1.0:
        return "-1"
    try:
        return str(int(round(float(score))))
    except (TypeError, ValueError):
        return ""


def _gradesheet_letter_map_from_rows(grade_rows) -> Dict[Tuple[str, str], str]:
    letter_map: Dict[Tuple[str, str], str] = {}
    for grade in grade_rows or []:
        sid = str(grade.get("student_id") or "").strip()
        lo_id = str(grade.get("learning_objective_id") or "").strip()
        letter = str(grade.get("top_score") or "").strip()
        if sid and lo_id and letter:
            if letter in ("M", "MR") and grade.get("counts_for_mastery") is False:
                letter = "I"
            letter_map[(sid, lo_id)] = letter
    return letter_map


def _gradesheet_csv_data_rows(
    students: List[Dict[str, Any]],
    learning_objectives: List[Dict[str, Any]],
    hw_map: Dict[str, Any],
    letter_map: Dict[Tuple[str, str], str],
    include_hw_prev: bool = False,
    prev_map: Optional[Dict[str, Any]] = None,
) -> List[List[str]]:
    rows: List[List[str]] = []
    prev_scores = prev_map or {}
    for student in students:
        sid = str(student.get("id") or "").strip()
        name = student.get("full_name") or ""
        hw_cell = _csv_format_hw_score(hw_map.get(sid))
        lo_cells = [
            letter_map.get((sid, str(lo.get("id") or "").strip()), "")
            for lo in learning_objectives
        ]
        if include_hw_prev:
            prev_cell = _csv_format_hw_score(prev_scores.get(sid))
            rows.append([name, prev_cell, hw_cell] + lo_cells)
        else:
            rows.append([name, hw_cell] + lo_cells)
    return rows


_BLANK_GRADESHEET_NOTE = (
    "This is a blank template. Grade columns are intentionally empty. "
    "Re-uploading this file will CLEAR any grades already entered for this assignment."
)


def _build_gradesheet_csv_text(
    assignment_name: str,
    date_value: str,
    vendor_codes: List[str],
    data_rows: List[List[str]],
    include_clear_warning: bool,
    include_hw_prev: bool = False,
) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(csv_safe_row(["Assignment", assignment_name]))
    writer.writerow(csv_safe_row(["Date", date_value]))
    if include_clear_warning:
        writer.writerow(csv_safe_row(["NOTE", _BLANK_GRADESHEET_NOTE]))
    writer.writerow(csv_safe_row([]))
    header = ["Student Name"]
    if include_hw_prev:
        header.append(Homework.HW_PREV_HEADER)
    header.append("HW")
    header.extend(vendor_codes)
    writer.writerow(csv_safe_row(header))
    for data_row in data_rows:
        writer.writerow(csv_safe_row(data_row))
    return "\ufeff" + output.getvalue()

def _enrolled_import_name_index(profiles: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Map a name key to every enrolled student id in this class.

    "Smith,Cali" and "Smith, Cali" are the same key. "Jane Doe" is not "Doe, Jane".
    """
    index: Dict[str, List[str]] = {}
    for p in profiles:
        sid = str(p.get("id") or "").strip()
        key = name_key(p.get("full_name") or "")
        if not sid or not key:
            continue
        ids = index.setdefault(key, [])
        if sid not in ids:
            ids.append(sid)
    return index


def _lookup_enrolled_import_student_id(
    csv_name: str, name_index: Dict[str, List[str]]
) -> Optional[str]:
    ids = name_index.get(name_key(csv_name)) or []
    if len(ids) == 1:
        return ids[0]
    return None
