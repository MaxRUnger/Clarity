import logging
from typing import Any, Dict, Optional, Tuple
from uuid import uuid4

from flask import jsonify, request

logger = logging.getLogger(__name__)


def _safe_api_error(message: str = "Request failed", status: int = 500, *, log_detail: Optional[Exception] = None):
    # Return a generic message to the client but include a correlation id so
    # the server log can be cross-referenced. Prevents accidentally leaking
    # internal exception details over the wire.
    err_id = str(uuid4())
    if log_detail is not None:
        logger.error("[%s] %s: %s", err_id, message, log_detail)
    return jsonify({"success": False, "error": message, "error_id": err_id}), status


def _require_json_object() -> Tuple[Optional[Dict[str, Any]], Optional[Tuple[Any, int]]]:
    """Parse JSON object body for state-changing APIs.

    Returns ``(data, None)`` on success, or ``(None, (response, status_code))`` on failure.

    Strict on purpose: a lot of routes used to call ``request.get_json(silent=True) or {}``,
    which silently turned a wrong content type or a JSON array into an "empty
    body" and then proceeded with whatever defaults the route had. That
    behavior masked real client bugs and accepted unexpected payloads, so
    state-changing endpoints route their body parsing through here instead.
    """
    if not request.is_json:
        return None, (jsonify({"success": False, "error": "Content-Type must be application/json"}), 415)
    try:
        data = request.get_json(force=False, silent=False)
    except Exception:
        return None, (jsonify({"success": False, "error": "Invalid JSON body"}), 400)
    if data is None:
        return None, (jsonify({"success": False, "error": "Invalid or empty JSON body"}), 400)
    if not isinstance(data, dict):
        return None, (jsonify({"success": False, "error": "JSON body must be an object"}), 400)
    return data, None
