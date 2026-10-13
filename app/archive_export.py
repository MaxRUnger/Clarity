"""Download-only class archive. Builds a zip in memory and deletes nothing."""

import csv
import io
import zipfile
from collections import Counter
from datetime import date
from typing import Any, Dict, List, Set, Tuple

import fitz

from app.authentication import supabase_admin
from app.models import Course, Homework
from app.paging import fetch_all_rows
from app.routes import (
    _build_gradesheet_csv_text,
    _csv_formula_safe,
    _gradesheet_csv_data_rows,
    _gradesheet_letter_map_for_assignment,
    _process_enrollments,
    build_learning_objectives_grid,
    learning_objective_column_name,
    objectives_attached_to_assignments,
)

_STEM_MAX = 80
_OBJECTIVES_ENTRY = "learning_objectives.csv"
_FORBIDDEN = set('\\/:*?"<>|')
_DEVICE_NAMES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)
_FONT_NAME = "FiraGO"
_BADGE_COLORS = {
    "m": ("#dcfce7", "#15803d", "#86efac"),
    "mr": ("#ecfdf5", "#047857", "#34d399"),
    "r": ("#fef9c3", "#a16207", "#fde047"),
    "rq": ("#ffedd5", "#c2410c", "#fdba74"),
    "p": ("#dbeafe", "#1d4ed8", "#93c5fd"),
    "x": ("#fee2e2", "#b91c1c", "#fca5a5"),
    "a": ("#f1f5f9", "#475569", "#cbd5e1"),
    "i": ("#fef3c7", "#92400e", "#fcd34d"),
}


class ArchiveClassNotFound(LookupError):
    """The class row was not found."""


class ArchiveManifestError(Exception):
    """The zip is missing an assignment CSV, a student PDF, or the objectives CSV."""


def archive_entry_stem(name: str, fallback: str) -> str:
    """Safe zip stem from a stored name."""
    chars = []
    for char in str(name or ""):
        if char in _FORBIDDEN or ord(char) < 32 or ord(char) == 127:
            chars.append("_")
        else:
            chars.append(char)
    stem = " ".join("".join(chars).split())
    stem = stem.rstrip(". ")
    if stem.upper() in _DEVICE_NAMES:
        stem = stem + "_"
    if not stem:
        stem = fallback
    if len(stem) > _STEM_MAX:
        stem = stem[:_STEM_MAX].rstrip(". ")
        if not stem:
            stem = fallback
    return stem


def _suffix_duplicate_stems(stems: List[Tuple[str, str]]) -> List[str]:
    """Add __ and the first 8 id characters to stems that repeat ignoring case."""
    counts = Counter(stem.casefold() for stem, _row_id in stems)
    return [
        f"{stem}__{str(row_id or '')[:8]}" if counts[stem.casefold()] > 1 else stem
        for stem, row_id in stems
    ]


def check_archive_manifest(manifest: Dict[str, Any], zip_bytes: bytes) -> None:
    """Raise when a listed entry is not in the zip or a count does not match its source rows."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        present = set(archive.namelist())
    assignment_entries = list(manifest.get("assignment_entries") or [])
    student_entries = list(manifest.get("student_entries") or [])
    problems = []
    if len(assignment_entries) != manifest.get("source_assignments"):
        problems.append("assignment CSVs do not match the assignments")
    if len(student_entries) != manifest.get("source_students"):
        problems.append("student PDFs do not match the enrolled students")
    expected = assignment_entries + student_entries + [manifest.get("objectives_entry")]
    missing = [str(name) for name in expected if name not in present]
    if missing:
        problems.append("missing from the zip: " + ", ".join(missing))
    if problems:
        raise ArchiveManifestError("; ".join(problems))


def _load_assignments(class_id: str) -> List[Dict[str, Any]]:
    rows = fetch_all_rows(
        supabase_admin.table("assignments")
        .select("id, name, date_returned, created_at")
        .eq("class_id", class_id)
        .order("id")
    )
    rows.sort(key=lambda row: (row.get("created_at") or "", str(row.get("id") or "")))
    return rows


def _objectives_for_assignment(pool: List[Dict[str, Any]], assignment_id: str) -> List[Dict[str, Any]]:
    linked = {
        str(lo_id).strip()
        for lo_id in (Course.get_assignment_lo_ids(assignment_id) or [])
        if str(lo_id).strip()
    }
    if not linked:
        return []
    return [
        lo for lo in pool
        if str(lo.get("id") or "").strip() in linked
    ]


def _rgb(hex_color: str) -> Tuple[float, float, float]:
    value = hex_color.lstrip("#")
    return tuple(int(value[index:index + 2], 16) / 255 for index in (0, 2, 4))


def _student_pdf(student: Dict[str, Any]) -> bytes:
    """One student sheet drawn like the printed progress page. No email and no id."""
    los = student.get("learning_objectives") or []
    passed = sum(1 for lo in los if lo.get("is_passed"))
    total = student.get("objective_total")
    if total is None:
        total = 0
    name = student.get("name") or student.get("full_name") or "Unnamed student"
    font_buffer = fitz.Font("figo").buffer
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_font(fontname=_FONT_NAME, fontbuffer=font_buffer)
    y = 54

    def new_page():
        nonlocal page, y
        page = doc.new_page(width=612, height=792)
        page.insert_font(fontname=_FONT_NAME, fontbuffer=font_buffer)
        y = 54

    def line(x, text, size, color="#0f172a"):
        page.insert_text((x, y), text, fontname=_FONT_NAME, fontsize=size, color=_rgb(color))

    line(36, name, 14)
    y += 22
    line(36, "Passed", 9, "#334155")
    line(260, "Progress", 9, "#334155")
    y += 16
    line(36, f"{passed} / {total}", 14)
    line(260, f"{passed}/{total}", 9, "#334155")
    bar_top = y + 4
    width = 160.0 * (passed / total) if total else 0
    page.draw_rect(fitz.Rect(260, bar_top, 420, bar_top + 4), color=None, fill=_rgb("#cbd5e1"), width=0)
    if width:
        page.draw_rect(
            fitz.Rect(260, bar_top, 260 + width, bar_top + 4),
            color=None,
            fill=_rgb("#16a34a"),
            width=0,
        )
    y = bar_top + 22

    for lo in los:
        if y > 760:
            new_page()
        label = lo.get("vendor_code") or lo.get("name") or "Objective"
        required = lo.get("required_ms")
        m_count = lo.get("m_count")
        if lo.get("is_passed"):
            pill = "PASSED"
            pill_fill, pill_color = "#16a34a", "#ffffff"
        else:
            pill = f"{m_count} / {required} Ms"
            pill_fill, pill_color = "#fef3c7", "#92400e"
        ms_text = f"{m_count} / {required} Ms"
        if lo.get("mr_count"):
            ms_text += f" ({lo['mr_count']} MR)"
        line(36, str(label), 9)
        pill_x = 150
        pill_w = max(52, 6 * len(pill) + 8)
        pill_rect = fitz.Rect(pill_x, y - 9, pill_x + pill_w, y + 2)
        page.draw_rect(pill_rect, color=None, fill=_rgb(pill_fill), width=0)
        page.insert_text(
            (pill_x + 4, y),
            pill,
            fontname=_FONT_NAME,
            fontsize=8,
            color=_rgb(pill_color),
        )
        x = pill_x + pill_w + 6
        meta_list = lo.get("grades_meta") or []
        for index, mark in enumerate(lo.get("grades_list") or []):
            mark_text = str(mark or "")
            if not mark_text:
                continue
            meta = meta_list[index] if index < len(meta_list) else None
            non_counting = (
                mark_text in ("M", "MR")
                and isinstance(meta, dict)
                and meta.get("counts_for_mastery") is False
            )
            if non_counting:
                fill, fg, border = "#fef2f2", "#b91c1c", "#ef4444"
            else:
                fill, fg, border = _BADGE_COLORS.get(mark_text.lower(), _BADGE_COLORS["a"])
            badge_w = max(14, 7 * len(mark_text) + 6)
            badge = fitz.Rect(x, y - 9, x + badge_w, y + 2)
            page.draw_rect(badge, color=_rgb(border), fill=_rgb(fill), width=0.6)
            page.insert_text((x + 3, y), mark_text, fontname=_FONT_NAME, fontsize=8, color=_rgb(fg))
            x += badge_w + 3
        page.insert_text((x + 4, y), ms_text, fontname=_FONT_NAME, fontsize=8, color=_rgb("#334155"))
        y += 14

    doc.subset_fonts()
    payload = doc.tobytes()
    doc.close()
    return payload


def _objectives_csv(columns: List[Dict[str, Any]], grid_rows: List[Dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    header = ["Student"]
    header.extend(_csv_formula_safe(column.get("vendor_code") or "") for column in columns)
    header.append("Progress")
    writer.writerow(header)
    for row in grid_rows:
        cells = [_csv_formula_safe(row.get("name") or "")]
        cells.extend(cell.get("display") or "" for cell in row.get("cells") or [])
        cells.append(row.get("progress_text") or "")
        writer.writerow(cells)
    return "\ufeff" + output.getvalue()


def _write_entry(archive: zipfile.ZipFile, name: str, payload: Any, used: Set[str]) -> None:
    parts = name.split("/")
    if not parts or ".." in parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError("unsafe zip entry")
    if name.casefold() in used:
        raise ArchiveManifestError(f"duplicate zip entry: {name}")
    used.add(name.casefold())
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    archive.writestr(name, payload)


def _assignment_csv(class_id: str, assignment: Dict[str, Any], pool, students) -> str:
    learning_objectives = _objectives_for_assignment(pool, assignment.get("id"))
    hw_map = Homework.get_hw_scores_map_for_assignment(class_id, assignment.get("id"))
    prev_map = Homework.get_hw_prev_scores_map_for_assignment(class_id, assignment.get("id"))
    letter_map = _gradesheet_letter_map_for_assignment(
        assignment.get("id"), students, learning_objectives
    )
    vendor_codes = [_csv_formula_safe(lo.get("vendor_code") or "") for lo in learning_objectives]
    data_rows = _gradesheet_csv_data_rows(
        students, learning_objectives, hw_map, letter_map, True, prev_map
    )
    return _build_gradesheet_csv_text(
        assignment.get("name") or "Assignment",
        assignment.get("date_returned") or date.today().isoformat(),
        vendor_codes,
        data_rows,
        False,
        True,
    )


def build_class_archive_zip(class_id: str) -> Tuple[bytes, Dict[str, Any]]:
    """Zip bytes and a manifest. Loads the class once. Deletes nothing."""
    class_data = Course.get_full_class_data(class_id)
    if not class_data:
        raise ArchiveClassNotFound(class_id)
    pool = list(class_data.get("learning_objectives") or [])
    assignments = _load_assignments(class_id)
    # Index 1 is every enrollment, including muted students. The pages use
    # index 0 and hide muted students. A muted student is still enrolled, and
    # this zip is the only copy of their grades once the class is deleted.
    students = _process_enrollments(class_data)[1]
    attached = objectives_attached_to_assignments(class_data.get("id"), pool) if class_data.get("id") else pool
    columns = [
        {
            "id": str(lo.get("id") or ""),
            "vendor_code": learning_objective_column_name(lo),
            "required_ms": lo.get("required_ms", 2),
        }
        for lo in attached
    ]
    grid_rows = build_learning_objectives_grid(students, columns)
    assignment_entries = [
        f"assignments/{stem}_gradesheet.csv"
        for stem in _suffix_duplicate_stems([
            (archive_entry_stem(assignment.get("name"), "assignment"), assignment.get("id"))
            for assignment in assignments
        ])
    ]
    student_entries = [
        f"students/{stem}_gradesheet.pdf"
        for stem in _suffix_duplicate_stems([
            (archive_entry_stem(student.get("name"), "student"), student.get("id"))
            for student in students
        ])
    ]
    used: Set[str] = set()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for assignment, entry in zip(assignments, assignment_entries):
            _write_entry(archive, entry, _assignment_csv(class_id, assignment, pool, students), used)
        for student, entry in zip(students, student_entries):
            _write_entry(archive, entry, _student_pdf(student), used)
        _write_entry(archive, _OBJECTIVES_ENTRY, _objectives_csv(columns, grid_rows), used)
    zip_bytes = buffer.getvalue()
    manifest = {
        "assignment_entries": assignment_entries,
        "student_entries": student_entries,
        "objectives_entry": _OBJECTIVES_ENTRY,
        "source_assignments": len(assignments),
        "source_students": len(students),
        "class_name": class_data.get("name") or "",
    }
    check_archive_manifest(manifest, zip_bytes)
    return zip_bytes, manifest
