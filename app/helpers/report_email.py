import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from typing import List, Optional, Tuple

import requests
from flask import current_app, session

from app.helpers.archive_snapshot import _IN_CHUNK_SIZE
from app.helpers.authentication import supabase_admin
from app.helpers.local_time import local_today
from app.helpers.paging import fetch_all_rows
from app.helpers.student_names import normalize_email

logger = logging.getLogger(__name__)

EMAIL_HOLD_HOURS = 24
EMAIL_INSTRUCTOR_DAILY_CAP = 400
EMAIL_RECIPIENT_DAILY_CAP = 5
EMAIL_MAX_RECIPIENTS = 100
REPORT_BODY_MAX = 5000
EMAIL_HOLD_MESSAGE = "This address was added or changed less than 24 hours ago."
EMAIL_INVALID_MESSAGE = "The saved email address is not valid."
EMAIL_RECIPIENT_CAP_MESSAGE = "The daily limit for this recipient has been reached."
EMAIL_INSTRUCTOR_CAP_MESSAGE = "The daily limit for this instructor has been reached."
EMAIL_CAP_UNAVAILABLE = (
    "Email could not be checked against the daily limit. Nothing was sent."
)
EMAIL_TOO_MANY = "Too many recipients in one request."


def instructor_title(full_name) -> str:
    """Professor <Last>, or Your instructor when the name is missing."""
    raw = "" if full_name is None else str(full_name)
    raw = " ".join(raw.split())
    if not raw:
        return "Your instructor"
    if "," in raw:
        last = raw.split(",", 1)[0].strip()
    else:
        last = raw.split(" ")[-1]
    if not last:
        return "Your instructor"
    if last.islower():
        last = last[:1].upper() + last[1:]
    return f"Professor {last}"


def _report_sender_name() -> str:
    """Full name of the logged-in instructor, from the profile row then the session."""
    name = str(session.get("full_name") or "").strip()
    uid = str(session.get("user_id") or "").strip()
    if not uid:
        return name
    try:
        resp = (
            supabase_admin.table("profiles")
            .select("full_name")
            .eq("id", uid)
            .limit(1)
            .execute()
        )
        row = (resp.data or [{}])[0] or {}
        return str(row.get("full_name") or "").strip() or name
    except Exception as e:
        logger.error("Report sender name lookup failed: %s", e)
        return name


def _reports_from_header(sender_name: str, from_email: str) -> str:
    title = str(sender_name or "")
    for ch in ('"', "\\", "<", ">", "\r", "\n"):
        title = title.replace(ch, "")
    title = " ".join(title.split())
    if not title:
        title = "Your instructor"
    return f'"{title} via Clarity Grader" <{from_email}>'


def _report_body_html(body_text: str, subject: str) -> str:
    """HTML part for a report email. Line breaks are tags, not CSS."""
    text = str(body_text or "").replace("\r\n", "\n").replace("\r", "\n")
    safe = html_escape(text, quote=True)
    blocks: List[List[str]] = []
    current: List[str] = []
    for line in safe.split("\n"):
        if line.strip() == "":
            if current:
                blocks.append(current)
                current = []
        else:
            current.append(line)
    if current:
        blocks.append(current)

    paragraph_style = (
        "font-family:Arial, Helvetica, sans-serif;"
        "font-size:14px;line-height:1.5;margin:0 0 14px 0;color:#222222;"
    )
    footer_style = (
        "font-family:Arial, Helvetica, sans-serif;"
        "font-size:12px;line-height:1.5;margin:0 0 14px 0;color:#666666;"
        "border-top:1px solid #dddddd;padding-top:12px;"
    )
    paragraphs: List[str] = []
    last = len(blocks) - 1
    for index, lines in enumerate(blocks):
        is_footer = index == last and (
            lines[0].lstrip().startswith("Sent by")
            or "Replies to this email are not monitored." in "\n".join(lines)
        )
        style = footer_style if is_footer else paragraph_style
        paragraphs.append(f'<p style="{style}">{"<br>".join(lines)}</p>')

    title = html_escape(str(subject or ""), quote=True)
    return (
        "<!DOCTYPE html>"
        '<html lang="en">'
        "<head>"
        '<meta charset="utf-8">'
        f"<title>{title}</title>"
        "</head>"
        "<body>"
        '<div style="max-width:600px;">'
        f"{''.join(paragraphs)}"
        "</div>"
        "</body>"
        "</html>"
    )


def _email_set_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def email_recipient_hash(address: str) -> str:
    return hashlib.sha256(address.encode("utf-8")).hexdigest()


def email_address_on_hold(email_set_at, now=None) -> bool:
    """True when the address was stored less than EMAIL_HOLD_HOURS ago."""
    if email_set_at is None:
        return False
    if isinstance(email_set_at, datetime):
        set_at = email_set_at
    else:
        text = str(email_set_at).strip()
        if not text:
            return False
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            set_at = datetime.fromisoformat(text)
        except ValueError:
            return True
    if set_at.tzinfo is None:
        set_at = set_at.replace(tzinfo=timezone.utc)
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - set_at) < timedelta(hours=EMAIL_HOLD_HOURS)


def _local_day_utc_bounds(today):
    from zoneinfo import ZoneInfo
    name = (current_app.config.get("APP_TIMEZONE") or "America/Los_Angeles").strip()
    tz = ZoneInfo(name or "America/Los_Angeles")
    start = datetime.combine(today, datetime.min.time()).replace(tzinfo=tz)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _response_count(resp) -> int:
    count = getattr(resp, "count", None)
    if isinstance(count, int):
        return count
    raise RuntimeError("email cap count unavailable")


def _count_email_sends(column: str, value: str, start, end) -> int:
    resp = (
        supabase_admin.table("email_sends")
        .select("id", count="exact")
        .eq(column, value)
        .gte("created_at", start.isoformat())
        .lt("created_at", end.isoformat())
        .limit(1)
        .execute()
    )
    return _response_count(resp)


def _email_cap_counts(instructor_id: str, hashes):
    """Return (instructor_count, hash_counts), or None when the read fails."""
    try:
        start, end = _local_day_utc_bounds(local_today())
        instructor_count = _count_email_sends("instructor_id", instructor_id, start, end)
        unique = list(dict.fromkeys(hashes))
        per_hash = {value: 0 for value in unique}
        for index in range(0, len(unique), _IN_CHUNK_SIZE):
            chunk = unique[index:index + _IN_CHUNK_SIZE]
            query = (
                supabase_admin.table("email_sends")
                .select("recipient_hash")
                .in_("recipient_hash", chunk)
                .gte("created_at", start.isoformat())
                .lt("created_at", end.isoformat())
                .order("id")
            )
            for row in fetch_all_rows(query):
                digest = str(row.get("recipient_hash") or "")
                if digest in per_hash:
                    per_hash[digest] += 1
    except Exception as exc:
        logger.error("email cap count failed user_id=%s: %s", instructor_id, exc)
        return None
    return instructor_count, per_hash


def _record_email_send(instructor_id, class_id, student_id, digest, kind) -> None:
    try:
        supabase_admin.table("email_sends").insert({
            "instructor_id": instructor_id,
            "class_id": class_id,
            "student_id": student_id,
            "recipient_hash": digest,
            "kind": kind,
        }).execute()
    except Exception:
        logger.error(
            "email_sends insert failed user_id=%s class_id=%s",
            instructor_id,
            class_id,
        )


def _report_footer(sender_name: str, class_title: str) -> str:
    lines = "\n".join([
        sender_name,
        "Class: %s" % class_title,
        "You are receiving this because you are enrolled in %s." % class_title,
        "Replies to this email are not monitored. Contact your instructor directly.",
    ])
    return "\n\n" + lines


_ADDRESS_KINDS = {
    "missing": ("No email address is saved for this student", 400),
    "invalid": (EMAIL_INVALID_MESSAGE, 400),
    "held": (EMAIL_HOLD_MESSAGE, 409),
}


def _saved_address_problem(raw_email, email_set_at):
    """Return (normalized address, kind). Kind is None when it may be sent."""
    raw = str(raw_email or "").strip()
    email = normalize_email(raw) if raw else None
    if not email:
        if raw:
            return None, "invalid"
        return None, "missing"
    if email_address_on_hold(email_set_at):
        return None, "held"
    return email, None


def _cap_block_reason(instructor_count, hash_count) -> Optional[str]:
    if instructor_count >= EMAIL_INSTRUCTOR_DAILY_CAP:
        return EMAIL_INSTRUCTOR_CAP_MESSAGE
    if hash_count >= EMAIL_RECIPIENT_DAILY_CAP:
        return EMAIL_RECIPIENT_CAP_MESSAGE
    return None


def _send_via_resend(
    to_email: str,
    subject: str,
    body_text: str,
    *,
    sender_name: str,
) -> Tuple[bool, str]:
    api_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    from_email = (os.environ.get("REPORTS_FROM_EMAIL") or "").strip()
    if not api_key:
        logger.error("Email send blocked: RESEND_API_KEY is not configured")
        return False, "Email is not configured."
    if not from_email:
        logger.error("Email send blocked: REPORTS_FROM_EMAIL is not configured")
        return False, "Email is not configured."

    payload = {
        "from": _reports_from_header(sender_name, from_email),
        "to": [to_email],
        "subject": subject,
        "text": body_text,
        "html": _report_body_html(body_text, subject),
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
        parsed = None
        try:
            parsed = resp.json()
        except ValueError:
            parsed = None
        message_id = ""
        if isinstance(parsed, dict) and isinstance(parsed.get("id"), str):
            message_id = parsed.get("id").strip()
        if resp.status_code >= 400 or not message_id:
            if resp.status_code >= 400:
                logger.error(
                    "Email provider rejected request (%s)",
                    resp.status_code,
                )
            else:
                logger.error(
                    "Email provider returned status %s without an id",
                    resp.status_code,
                )
            return False, "Email provider rejected request."
        domain = to_email.rsplit("@", 1)[-1] if "@" in to_email else "unknown"
        logger.info(
            "Report email accepted id=%s recipient_domain=%s",
            message_id,
            domain,
        )
        return True, ""
    except Exception as e:
        logger.error("Email send failed: %s", e)
        return False, "Email send failed."
