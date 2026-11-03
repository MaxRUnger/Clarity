"""Calendar dates accepted from assignment create and update."""

import re
from datetime import date

_CALENDAR_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
DATE_MESSAGE = "Enter a date as YYYY-MM-DD."


def clean_calendar_date(value):
    """Return (stored value, error message). Empty input stores None."""
    if value is None or value == "":
        return None, None
    if not isinstance(value, str) or _CALENDAR_DATE.fullmatch(value) is None:
        return None, DATE_MESSAGE
    try:
        date.fromisoformat(value)
    except ValueError:
        return None, DATE_MESSAGE
    return value, None


def clean_assignment_dates(data):
    """Return (date_returned, revision_due, error message)."""
    date_returned, date_error = clean_calendar_date(data.get("date_returned"))
    if date_error:
        return None, None, date_error
    revision_due, due_error = clean_calendar_date(data.get("revision_due"))
    if due_error:
        return None, None, due_error
    return date_returned, revision_due, None
