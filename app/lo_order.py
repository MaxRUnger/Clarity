"""Natural order for learning-objective labels.

Text chunks compare case-insensitively. Digit chunks compare as integers, so
AO2 sorts before AO10. Whitespace is ignored, so "A 2" and "A2" share a key.
A row with no vendor code and no name sorts last. Equal labels break ties on id.
"""

import re

_CHUNK = re.compile(r"(\d+)")
_MISSING_WHEN_NO_CODE = frozenset({"unknown lo", "objective"})


def _objective_source(lo):
    """Objective fields, or the row itself when it already is the objective.

    Assignment links nest the objective under learning_objectives.
    """
    if isinstance(lo, dict):
        nested = lo.get("learning_objectives")
        if isinstance(nested, dict):
            return nested
    return lo


def lo_display_label(lo) -> str:
    source = _objective_source(lo)
    if isinstance(source, str):
        return source.strip()
    if not isinstance(source, dict):
        return ""
    vendor = str(source.get("vendor_code") or "").strip()
    if vendor:
        return vendor
    name = str(source.get("name") or "").strip()
    if name.casefold() in _MISSING_WHEN_NO_CODE:
        return ""
    return name


def lo_sort_key(lo):
    label = "".join(lo_display_label(lo).casefold().split())
    missing = 0 if label else 1
    chunks = []
    for piece in _CHUNK.split(label):
        if not piece:
            continue
        if piece.isdigit():
            chunks.append((0, int(piece)))
        else:
            chunks.append((1, piece))
    source = _objective_source(lo)
    if isinstance(lo, dict):
        lo_id = str(lo.get("id") or lo.get("learning_objective_id") or "")
        if not lo_id and isinstance(source, dict):
            lo_id = str(source.get("id") or "")
    else:
        lo_id = ""
    return (missing, tuple(chunks), lo_id)
