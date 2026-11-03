from typing import Any, Dict

from app.helpers.student_names import display_name

def lo_cell_status(m_count, required):
    """passed when m >= r, close when one mastery short, otherwise behind."""
    m = 0 if m_count is None else m_count
    r = 0 if required is None else required
    if m >= r:
        return "passed"
    if m >= 1 and (r - m) == 1:
        return "close"
    return "behind"


def learning_objective_column_name(lo: Dict[str, Any]) -> str:
    """Header the Learning Objectives table shows for one objective."""
    return lo.get("vendor_code") or lo.get("name") or "Objective"


def learning_objective_cell_text(m_count, required) -> str:
    """Visible cell: a check mark when passed, otherwise m/required."""
    if lo_cell_status(m_count, required) == "passed":
        return "✓"
    return f"{m_count}/{required}"


def learning_objective_progress_text(passed, total) -> str:
    """Progress column: passed objectives over the attached total."""
    return f"{passed}/{total}"


def build_learning_objectives_grid(students, learning_objectives):
    """Rows for the Learning Objectives table. Cell text comes from the helpers above."""
    grid_rows = []
    for student in students:
        by_lo = {
            str(slo.get("learning_objective_id") or ""): slo
            for slo in (student.get("learning_objectives") or [])
        }
        cells = []
        for row in learning_objectives:
            matched = by_lo.get(row["id"])
            if matched is None:
                m_count = 0
                required = row["required_ms"]
            else:
                m_count = int(matched.get("m_count") or 0)
                required = matched.get("required_ms")
                if required is None:
                    required = row["required_ms"]
            cells.append({
                "text": f"{m_count}/{required}",
                "status": lo_cell_status(m_count, required),
                "display": learning_objective_cell_text(m_count, required),
            })
        student_los = student.get("learning_objectives") or []
        passed = sum(1 for slo in student_los if slo.get("is_passed"))
        objective_total = student.get("objective_total")
        if objective_total is None:
            objective_total = 0
        grid_rows.append({
            "name": display_name(student.get("full_name") or ""),
            "cells": cells,
            "learning_objectives": student_los,
            "objective_total": objective_total,
            "progress_text": learning_objective_progress_text(passed, objective_total),
        })
    return grid_rows
