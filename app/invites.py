"""Signup invite codes stored as SHA-256 hashes."""

import hashlib
import logging
from datetime import datetime, timezone

from app.authentication import supabase_admin

logger = logging.getLogger(__name__)


def invite_code_hash(code: str) -> str:
    """Lowercase hex SHA-256 of the invite code after strip()."""
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def _in_the_future(value) -> bool:
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment > datetime.now(timezone.utc)


def _below_max(used, maximum) -> bool:
    try:
        return int(used) < int(maximum)
    except (TypeError, ValueError):
        return False


def invite_code_is_valid(code) -> bool:
    """True only for a live, unexpired invite that still has uses left."""
    if not isinstance(code, str) or code.strip() == "":
        return False
    digest = invite_code_hash(code)
    try:
        response = (
            supabase_admin.table("signup_invites")
            .select("revoked_at, expires_at, used_count, max_uses")
            .eq("code_hash", digest)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        logger.error("signup invite lookup failed: %s", type(exc).__name__)
        return False
    rows = getattr(response, "data", None) or []
    if not rows or not isinstance(rows[0], dict):
        return False
    row = rows[0]
    if row.get("revoked_at"):
        return False
    expires_at = row.get("expires_at")
    if expires_at is not None and not _in_the_future(expires_at):
        return False
    return _below_max(row.get("used_count"), row.get("max_uses"))
