from typing import Any, Dict, List

from app.helpers.authentication import supabase_admin
from app.models import ASSIGNMENT_TYPES


def _insert_assignment_objectives(ao_rows: List[Dict[str, Any]]) -> None:
    """Insert assignment_objectives rows."""
    if not ao_rows:
        return
    supabase_admin.table("assignment_objectives").insert(ao_rows).execute()


def _assignment_type_fields_from_payload(data: Dict[str, Any]) -> str:
    """Parse assignment_type from create/update body.

    Invalid / missing type falls back to 'mastery_opp'.
    'project' is a valid stored label, separate from 'exam'.
    """
    raw = data.get("assignment_type") or "mastery_opp"
    if not isinstance(raw, str):
        raw = "mastery_opp"
    return raw.strip() if raw.strip() in ASSIGNMENT_TYPES else "mastery_opp"
