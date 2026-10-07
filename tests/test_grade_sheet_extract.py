"""Tests for local PDF grade-sheet extraction and header canonicalization.

These tests pin down the contract between ``gemini_analyzer.analyze_pdf`` and
``grade_sheet_pdf_extract``: a vector PDF with an unambiguous table must be
parsed locally without ever calling the vision API, and the resulting payload
must already be in the shape that ``_normalize_data`` expects.
"""

import pytest

from app.models import Homework

try:
    import fitz

    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False

from app.dao.grade_sheet_pdf_extract import try_extract_grade_sheet_from_pdf_bytes
from app.dao.gemini_analyzer import GradeSheetGeminiAnalyzer


@pytest.mark.skipif(not HAS_FITZ, reason="PyMuPDF (fitz) required")
def test_try_extract_grid_pdf_find_tables():
    # PyMuPDF's `find_tables` detects table structure from ruling lines, not
    # from text positions. We draw explicit row/column lines below so the
    # fast-path actually fires; a synthetic PDF with text alone gets routed
    # through the gap-grid fallback and would test a different code path.
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    x0, y0, x1, y1 = 50, 50, 520, 180
    rows, cols = 3, 4
    for i in range(rows + 1):
        y = y0 + i * (y1 - y0) / rows
        page.draw_line((x0, y), (x1, y), width=0.5, color=(0, 0, 0))
    for j in range(cols + 1):
        x = x0 + j * (x1 - x0) / cols
        page.draw_line((x, y0), (x, y1), width=0.5, color=(0, 0, 0))
    cells = [
        ["Name", "EX1", "A7", "HW"],
        ["Jane Doe", "88", "M", "90"],
        ["Bob Smith", "72", "P", "85"],
    ]
    for ri, row in enumerate(cells):
        for ci, val in enumerate(row):
            x = x0 + 8 + ci * (x1 - x0) / cols
            y = y0 + 14 + ri * (y1 - y0) / rows
            page.insert_text((x, y), val, fontsize=10)
    pdf_bytes = doc.tobytes()
    doc.close()

    pack = try_extract_grade_sheet_from_pdf_bytes(pdf_bytes)
    assert pack is not None
    assert pack.get("extraction_path") == "pdf_table"
    assert pack["learning_objectives"] == ["EX1", "A7", "HW"]
    assert len(pack["students"]) == 2
    by_name = {s["name"]: s for s in pack["students"]}
    assert by_name["Jane Doe"]["grades"].get("EX1") == "88"
    assert by_name["Jane Doe"]["grades"].get("A7") == "M"


@pytest.mark.skipif(not HAS_FITZ, reason="PyMuPDF (fitz) required")
def test_try_extract_blank_pdf_returns_none():
    doc = fitz.open()
    doc.new_page()
    b = doc.tobytes()
    doc.close()
    assert try_extract_grade_sheet_from_pdf_bytes(b) is None


def test_canonicalize_import_sheet_header():
    assert Homework.canonicalize_import_sheet_header("Exam 1") == "EX1"
    assert Homework.canonicalize_import_sheet_header("EX 2") == "EX2"
    assert Homework.canonicalize_import_sheet_header("  LO 12  ") == "LO12"
    assert Homework.canonicalize_import_sheet_header("A 7") == "A7"
    assert Homework.canonicalize_import_sheet_header("EX1") == "EX1"


def test_normalize_data_canonicalizes_column_keys():
    # "Exam 1" canonicalizes to "EX1" (see canonicalize_import_sheet_header), but since
    # the exam-score feature is removed, EX1 is now just a plain LO/grade column.
    a = object.__new__(GradeSheetGeminiAnalyzer)
    raw = {
        "learning_objectives": ["Exam 1", "A7"],
        "students": [{"name": "Jane Doe", "grades": {"Exam 1": "88", "A7": "M"}}],
    }
    out = GradeSheetGeminiAnalyzer._normalize_data(a, raw)
    assert out["learning_objectives"] == ["EX1", "A7"]
    assert out["students"][0]["grades"].get("EX1") == "88"
    assert out["students"][0]["grades"].get("A7") == "M"


def test_normalize_data_skips_hw_prev_as_score_and_objective():
    a = object.__new__(GradeSheetGeminiAnalyzer)
    raw = {
        "learning_objectives": ["HW", "HW prev", "D1"],
        "students": [{
            "name": "Jane Doe",
            "grades": {"HW": "80", "HW prev": "70", "D1": "M"},
        }],
    }
    out = GradeSheetGeminiAnalyzer._normalize_data(a, raw)
    assert out["students"][0]["homework_pct"] == "80"
    assert "HW prev" not in out["learning_objectives"]
    assert "HW prev" not in out["students"][0]["grades"]
    assert out["students"][0]["grades"].get("D1") == "M"


def test_normalize_data_duplicate_headers_merge_first_nonempty():
    a = object.__new__(GradeSheetGeminiAnalyzer)
    raw = {
        "learning_objectives": ["EX1", "Exam 1"],
        "students": [{"name": "Jane Doe", "grades": {"EX1": "", "Exam 1": "91"}}],
    }
    out = GradeSheetGeminiAnalyzer._normalize_data(a, raw)
    assert out["students"][0]["grades"].get("EX1") == "91"
