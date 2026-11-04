import logging
from typing import Any, Dict, List, Tuple
from uuid import uuid4

from app.helpers.authentication import supabase_admin
from app.models import DEFAULT_REQUIRED_MS

logger = logging.getLogger(__name__)


def _select_class_pool_learning_objectives(select_cols: str, class_id: str):
    """Select learning objectives for a class."""
    return (
        supabase_admin.table("learning_objectives")
        .select(select_cols)
        .eq("class_id", class_id)
        .execute()
    )


def annotate_learning_objective_rows_for_preview(
    class_id: str, rows: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Tag each row with duplicate=true if vendor_code already exists in the class."""
    try:
        existing_resp = _select_class_pool_learning_objectives(
            "vendor_code", class_id
        )
        existing = {
            (r.get("vendor_code") or "").strip().lower()
            for r in (existing_resp.data or [])
            if (r.get("vendor_code") or "").strip()
        }
    except Exception:
        existing = set()
    out = []
    for row in rows:
        r = dict(row)
        vc = (r.get("vendor_code") or "").strip()
        r["duplicate"] = bool(vc and vc.lower() in existing)
        out.append(r)
    return out


def _insert_learning_objectives(payload: Any):
    """Insert LO rows. name is the given name, or the vendor code when none is given."""
    rows = payload if isinstance(payload, list) else [payload]
    named = []
    for row in rows:
        item = dict(row or {})
        name = str(item.get("name") or "").strip()
        if not name:
            name = str(item.get("vendor_code") or "").strip()
        item["name"] = name
        named.append(item)
    body = named if isinstance(payload, list) else named[0]
    return supabase_admin.table("learning_objectives").insert(body).execute()


def import_learning_objectives_rows(class_id: str, rows: List[Dict[str, Any]]) -> Tuple[int, int, List[str]]:
    """Insert new LOs; skip vendor_codes that already exist (case-insensitive). Returns (inserted, skipped, errors)."""
    errors: List[str] = []
    if not rows:
        return 0, 0, errors
    try:
        existing_resp = _select_class_pool_learning_objectives(
            "vendor_code", class_id
        )
        existing = {
            (r.get("vendor_code") or "").strip().lower()
            for r in (existing_resp.data or [])
            if (r.get("vendor_code") or "").strip()
        }
    except Exception as e:
        logger.error("Failed to load existing objectives: %s", e)
        return 0, 0, ["Failed to load existing objectives."]

    inserted = 0
    skipped = 0
    batch: List[Dict[str, Any]] = []
    chunk = 80

    for row in rows:
        vc = (row.get("vendor_code") or "").strip()
        if not vc:
            continue
        key = vc.lower()
        if key in existing:
            skipped += 1
            continue
        existing.add(key)
        batch.append(
            {
                "class_id": class_id,
                "vendor_code": vc,
                "description": row.get("description"),
                "required_ms": int(row.get("required_ms") or DEFAULT_REQUIRED_MS),
            }
        )

    for i in range(0, len(batch), chunk):
        piece = batch[i : i + chunk]
        if not piece:
            continue
        try:
            _insert_learning_objectives(piece)
            inserted += len(piece)
        except Exception as e:
            err_id = str(uuid4())
            logger.error("[%s] LO batch insert failed: %s", err_id, e)
            errors.append(f"Import failed (ref {err_id}).")
            break
    return inserted, skipped, errors


def _lo_vendor_code_conflict(class_id: str, vendor_code: str, exclude_lo_id: str) -> bool:
    """True if another LO in the class already uses this vendor_code (case-insensitive)."""
    vc = (vendor_code or "").strip()
    if not vc:
        return False
    try:
        resp = _select_class_pool_learning_objectives("id, vendor_code", class_id)
    except Exception:
        return True
    ex = str(exclude_lo_id).strip()
    vcl = vc.lower()
    for row in resp.data or []:
        if str(row.get("id") or "").strip() == ex:
            continue
        other = (row.get("vendor_code") or "").strip()
        if other and other.lower() == vcl:
            return True
    return False
