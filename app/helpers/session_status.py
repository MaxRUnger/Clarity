import time

from flask import current_app, jsonify, request
from itsdangerous import BadSignature


def _session_seconds_left():
    raw = request.cookies.get(current_app.config["SESSION_COOKIE_NAME"])
    serializer = current_app.session_interface.get_signing_serializer(current_app)
    if not raw or serializer is None:
        return None
    max_age = int(current_app.permanent_session_lifetime.total_seconds())
    try:
        _payload, issued = serializer.loads(raw, max_age=max_age, return_timestamp=True)
    except BadSignature:
        return None
    issued_at = issued.timestamp() if hasattr(issued, "timestamp") else float(issued)
    remaining = max_age - int(time.time() - issued_at)
    return remaining if remaining > 0 else 0


def _session_status_response(body, status):
    response = jsonify(body)
    response.headers["Cache-Control"] = "no-store"
    response.status_code = status
    return response
