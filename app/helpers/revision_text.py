from datetime import date
from typing import Any, Dict

from flask import session

_REVISION_FOOTER = (
    "Replies to this email are not monitored. "
    "Contact your instructor directly."
)
_REVISION_SCORE_PHRASE = {
    "R": "Revise for Mastery",
    "RQ": "Revise with Question (RQ)",
}


def _revision_due_phrase(iso_date: str) -> str:
    year, month, day = [int(part) for part in str(iso_date).split("-")]
    written = date(year, month, day)
    return written.strftime("%A, %b ") + str(written.day)


def _revision_greeting(student_name: str) -> str:
    text = " ".join(str(student_name or "").split())
    if not text:
        return "student"
    if "," not in text:
        return text
    given = " ".join(text.split(",", 1)[1].split())
    return given or text


def _revision_signoff(full_name: str) -> str:
    """Natural order. Signup stores the typed name, often Last, First."""
    text = " ".join(str(full_name or "").split())
    if not text:
        return "Your instructor"
    if "," not in text:
        return text
    last, given = text.split(",", 1)
    last = " ".join(last.split())
    given = " ".join(given.split())
    if given and last:
        return f"{given} {last}"
    return given or last or "Your instructor"


def _revision_objective_line(obj: Dict[str, Any]) -> str:
    parts = []
    code = str(obj.get("lo_code") or "").strip()
    if code:
        parts.append(code)
    if obj.get("show_name"):
        parts.append(str(obj.get("lo_name") or "").strip())
    label = " ".join(part for part in parts if part) or "Objective"
    mark = str(obj.get("top_score") or "").strip()
    return f"{label}: {_REVISION_SCORE_PHRASE.get(mark, mark)}"


def _revision_message(
    student: Dict[str, Any], due_iso: str, class_title: str, signature: str
) -> str:
    lines = [
        f"Hello {_revision_greeting(student.get('name') or '')},",
        "",
        f"You have a revision due in 3 days, on {_revision_due_phrase(due_iso)}.",
        f"Class: {class_title}",
        "",
        "Please revise the following:",
        "",
    ]
    assignments = list(student.get("assignments") or [])
    for index, assignment in enumerate(assignments):
        if index:
            lines.append("")
        name = str(assignment.get("assignment_name") or "Assignment")
        lines.append(f"Assignment: {name}")
        for obj in assignment.get("objectives") or []:
            lines.append("- " + _revision_objective_line(obj))
    lines.extend(["", "Thank you,", signature, "", _REVISION_FOOTER])
    return "\n".join(lines)


def _revision_sender_name() -> str:
    return _revision_signoff(str(session.get("full_name") or ""))
