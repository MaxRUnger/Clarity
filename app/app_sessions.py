"""Server-side session rows addressed by the hash of the cookie token."""
import hashlib
import logging
import threading
from datetime import datetime, timedelta, timezone
from time import monotonic as _clock

from flask import jsonify, redirect, request, session, url_for
from werkzeug.exceptions import MethodNotAllowed

logger = logging.getLogger(__name__)

CACHE_LIMIT = 500
CACHE_SECONDS = 30
_ABSOLUTE_HOURS = 12
_STALE_DAYS = 7

_cache = {}
_lock = threading.Lock()


def _admin():
    from app.routes import supabase_admin
    return supabase_admin


def _as_utc(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def clear_app_session_cache():
    with _lock:
        _cache.clear()


def app_session_cache_size():
    with _lock:
        return len(_cache)


def remember_app_session(token_hash, user_id, expires_at, now):
    expires = _as_utc(expires_at)
    with _lock:
        stale = [
            key
            for key, (cached_at, _user, exp) in _cache.items()
            if now - cached_at >= CACHE_SECONDS or exp <= datetime.now(timezone.utc)
        ]
        for key in stale:
            del _cache[key]
        if token_hash not in _cache and len(_cache) >= CACHE_LIMIT:
            oldest = min(_cache, key=lambda key: _cache[key][0])
            del _cache[oldest]
        _cache[token_hash] = (now, str(user_id), expires)


def cached_app_session(token_hash, now):
    with _lock:
        item = _cache.get(token_hash)
        if item is None:
            return None
        cached_at, user_id, expires_at = item
        if expires_at <= datetime.now(timezone.utc) or now - cached_at >= CACHE_SECONDS:
            del _cache[token_hash]
            return None
        return (user_id, expires_at)


def _token_hash(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _fetch_row(token_hash):
    response = (
        _admin()
        .table("app_sessions")
        .select("token_hash, user_id, expires_at, revoked_at")
        .eq("token_hash", token_hash)
        .limit(1)
        .execute()
    )
    rows = response.data
    if not isinstance(rows, list):
        raise RuntimeError("app_sessions lookup returned no row list")
    return rows[0] if rows else None


def _row_is_live(row, user_id):
    if not isinstance(row, dict):
        return False
    if str(row.get("user_id") or "") != str(user_id or ""):
        return False
    if row.get("revoked_at"):
        return False
    try:
        expires = _as_utc(row.get("expires_at"))
    except (TypeError, ValueError):
        return False
    return expires > datetime.now(timezone.utc)


def _token_is_live(token, user_id):
    if not token:
        return False
    token_hash = _token_hash(token)
    now = _clock()
    cached = cached_app_session(token_hash, now)
    if cached is not None:
        return cached[0] == str(user_id)
    try:
        row = _fetch_row(token_hash)
    except Exception as exc:
        logger.error("app_sessions lookup failed user_id=%s: %s", user_id, exc)
        return False
    if not _row_is_live(row, user_id):
        return False
    remember_app_session(token_hash, user_id, row.get("expires_at"), now)
    return True


_EXEMPT = {
    "static",
    "main.login_page",
    "main.signup_page",
    "main.login",
    "main.signup",
    "main.logout",
}


def validate_app_session():
    if isinstance(getattr(request, "routing_exception", None), MethodNotAllowed):
        return None
    endpoint = (request.endpoint or "").strip()
    if endpoint in _EXEMPT:
        return None
    user_id = session.get("user_id")
    if not user_id:
        return None
    if _token_is_live(session.get("session_token"), user_id):
        return None
    session.clear()
    if request.path.startswith("/api"):
        body = jsonify({
            "success": False,
            "error": "Unauthorized",
            "code": "session_expired",
        })
        body.status_code = 401
        return body
    return redirect(url_for("main.login_page"))


def revoke_live_session(token, user_id):
    if not token or not user_id:
        return
    token_hash = _token_hash(token)
    row = _fetch_row(token_hash)
    if not _row_is_live(row, user_id):
        return
    _admin().table("app_sessions").update({
        "revoked_at": datetime.now(timezone.utc).isoformat(),
    }).eq("token_hash", token_hash).execute()
    with _lock:
        _cache.pop(token_hash, None)


def delete_stale_sessions():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_STALE_DAYS)).isoformat()
    _admin().table("app_sessions").delete().lt("expires_at", cutoff).execute()


def new_session_token():
    import secrets
    return secrets.token_urlsafe(32)


def absolute_expiry():
    return datetime.now(timezone.utc) + timedelta(hours=_ABSOLUTE_HOURS)
