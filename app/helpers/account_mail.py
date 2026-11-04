import logging
import os

import requests
from flask import jsonify

from config import Config
from app.helpers.authentication import new_anon_client, supabase_admin

logger = logging.getLogger(__name__)


def _public_base_url():
    """Return the configured public base URL, or an empty string."""
    return (Config.PUBLIC_BASE_URL or "").strip().rstrip("/")


def _forgot_ok():
    return jsonify({
        "success": True,
        "message": "If an instructor account exists for that address, a reset message is on its way.",
    })


def _profile_role(user_id):
    try:
        response = (
            supabase_admin.table("profiles")
            .select("role")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        logger.error("Password reset profile lookup failed: %s", type(exc).__name__)
        return ""
    rows = response.data or []
    if not rows:
        return ""
    return str((rows[0] or {}).get("role") or "").strip().lower()


def _send_account_email(to_email: str, subject: str, body_text: str) -> bool:
    api_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    from_email = (os.environ.get("REPORTS_FROM_EMAIL") or "").strip()
    if not api_key or not from_email:
        logger.error("Account email blocked: mail is not configured")
        return False
    payload = {
        "from": f'"Clarity Grader" <{from_email}>',
        "to": [to_email],
        "subject": subject,
        "text": body_text,
    }
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
    except Exception as exc:
        logger.error("Account email failed: %s", type(exc).__name__)
        return False
    if resp.status_code >= 400:
        logger.error("Account email rejected status %s", resp.status_code)
        return False
    return True


def verify_recovery_token(token_hash):
    client = new_anon_client()
    return client.auth.verify_otp({"token_hash": token_hash, "type": "recovery"})
