CLASS_DAYS = ("MW", "TTH", "MWF", "WF", "Asynchronous")
SECTION_NUMBER_MAX = 32


def _clean_class_days(raw):
    """Accept only a listed meeting pattern. Blank is not a listed value."""
    days = (raw or "").strip()
    if days not in CLASS_DAYS:
        return None, "Class days must be one of the listed options."
    return days, None


def class_display_title(name, days, section):
    """Join the non-empty class name, days, and section with a separator."""
    parts = []
    for value in (name, days, section):
        text = "" if value is None else str(value).strip()
        if text:
            parts.append(text)
    return " | ".join(parts)


def _clean_section_number(raw):
    """Trim a section number. Blank becomes null. Case is kept."""
    section = (raw or "").strip()
    if not section:
        return None, None
    if len(section) > SECTION_NUMBER_MAX:
        return None, "Section number must be 32 characters or fewer."
    return section, None


def _clean_class_form(form):
    """Validate the shared class form. Returns (values, error_message)."""
    raw_name = form.get("name")
    name = "" if raw_name is None else str(raw_name).strip()
    if not name:
        return None, "Class name is required."
    if len(name) > 255:
        return None, "Class name must be 255 characters or fewer."
    section_number, section_error = _clean_section_number(form.get("section_number"))
    if section_error:
        return None, section_error
    days, days_error = _clean_class_days(form.get("days"))
    if days_error:
        return None, days_error
    semester = form.get("semester", "") or ""
    year = form.get("year", "") or ""
    semester_full = f"{semester} {year}".strip() if year else semester
    if len(semester_full) > 100:
        return None, "Semester must be 100 characters or fewer."
    return {
        "name": name,
        "semester": semester_full,
        "days": days,
        "section_number": section_number,
        "is_online": form.get("is_online") == "1",
        "auto_convert_m": form.get("auto_convert_m") == "1",
    }, None
