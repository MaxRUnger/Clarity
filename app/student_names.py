"""One stored student name: the Canvas string "Last, First"."""

from typing import Any, Optional
import re

import pyuca

_COLLATOR = pyuca.Collator()
_BLANK_SORT = "\uffff"

COMMA_REQUIRED = (
    "Enter the name as Last, First, with a comma. Example: Velasco Jr, Emilio."
)

_SIMPLE_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_EMAIL_MAX = 255


def normalize_spaces(text: str) -> str:
    """Trim and collapse internal whitespace to a single space."""
    return " ".join((text or "").strip().split())


def name_key(name: str) -> str:
    """Match key. Lowercase, whitespace collapsed, including the spaces beside a comma."""
    text = normalize_spaces(name).lower()
    if "," not in text:
        return text
    parts = [normalize_spaces(part) for part in text.split(",")]
    return ", ".join(parts)


def require_canvas_name(raw: str) -> Optional[str]:
    """Return "Last, First", or None when blank or either side of the comma is empty."""
    text = normalize_spaces(raw)
    if "," not in text:
        return None
    last, first = text.split(",", 1)
    last = normalize_spaces(last)
    first = normalize_spaces(first)
    if not last or not first:
        return None
    return f"{last}, {first}"


def normalize_email(raw: Optional[str]) -> Optional[str]:
    """Return a lowercased email, or None when blank, too long, or invalid."""
    text = (raw or "").strip().lower()
    if not text or len(text) > _EMAIL_MAX:
        return None
    if not _SIMPLE_EMAIL_RE.match(text):
        return None
    return text


def raw_name_from_csv_fields(first: str, last: str) -> str:
    """Build the Canvas string from separate first and last columns."""
    return f"{normalize_spaces(last)}, {normalize_spaces(first)}"


def display_name(full_name: str) -> str:
    """Return the stored name unchanged, or a stand-in when it is blank."""
    if not (full_name or "").strip():
        return "Unnamed student"
    return full_name


def collator_key(full_name: str) -> Any:
    """pyuca sort key for the stored name. A blank name sorts last."""
    text = full_name or ""
    if not text.strip():
        return _COLLATOR.sort_key(_BLANK_SORT)
    return _COLLATOR.sort_key(text)


def row_sort_key(student: dict) -> Any:
    """Sort key for a student dict. Uses full_name only."""
    return collator_key(student["full_name"])
