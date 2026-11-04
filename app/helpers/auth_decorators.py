from functools import wraps

from flask import jsonify, redirect, session, url_for


# ============================================================================
# AUTH DECORATORS
# ============================================================================

def login_required(f):
    """Redirect to login page if the user has no active session."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('main.login_page'))
        return f(*args, **kwargs)
    return decorated


def _session_expired_response():
    return jsonify({
        "success": False,
        "error": "Unauthorized",
        "code": "session_expired",
    }), 401


def api_login_required(f):
    """Return 401 JSON if the user has no active session (for API routes)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return _session_expired_response()
        return f(*args, **kwargs)
    return decorated


def api_instructor_required(f):
    """Return 401 JSON if the user is not a logged-in instructor."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return _session_expired_response()
        role = (session.get("role") or "").strip().lower()
        if role != "instructor":
            return jsonify({"success": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated
