"""One spreadsheet-formula guard for every CSV cell this app writes."""

import re


def csv_formula_safe(value) -> str:
    """Prefix one apostrophe when a spreadsheet would treat the cell as a formula.

    Leading space, tab, CR, and LF are skipped before the check. A plain
    negative number is left unchanged. Every other cell is left unchanged.
    """
    if value is None:
        return ""
    text = str(value)
    if text == "":
        return text
    probe = text.lstrip(" \t\r\n")
    if probe == "":
        return text
    first = probe[0]
    if first in ("=", "+", "@"):
        return "'" + text
    if first == "-":
        if re.fullmatch(r"-\d+(\.\d+)?", probe):
            return text
        return "'" + text
    return text


def csv_safe_row(row) -> list:
    return [csv_formula_safe(cell) for cell in row]
