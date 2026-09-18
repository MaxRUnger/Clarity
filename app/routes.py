"""HTTP routes and API handlers for Clarity.

Deployment (production)
-----------------------
In-memory structures in this module — mobile upload handoff
(``_pending_mobile_uploads``), request rate limits (``_rate_limit_events``), and
one-time form tokens (``_used_form_tokens``) — are **not** shared across
Gunicorn/Uvicorn workers. With multiple workers, QR phone uploads, rate limits,
and token replay checks can appear broken or inconsistent.

**Options:** run **one worker**; use a load balancer with **sticky sessions** to
the same worker; or replace these stores with **Redis** (or equivalent) before
scaling horizontally.

If you intentionally run multiple workers in production, set environment
variable ``MULTI_WORKER=1`` to log a one-time warning at startup (see
``create_app`` in ``app/__init__.py``).
"""

import csv
import hmac
import io
import logging
import os
import pyuca
import re
import secrets
import requests
import socket
import threading
import time
from datetime import date
from collections import defaultdict, deque
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Tuple
from flask import (  # type: ignore
    Blueprint,
    render_template,
    request,
    jsonify,
    redirect,
    url_for,
    session,
    abort,
    Response,
    send_file,
    g,
)
from werkzeug.utils import secure_filename
from app.authentication import supabase, supabase_admin
from app.models import (
    Course,
    Grade,
    Student,
    Homework,
    ASSIGNMENT_TYPES,
)
from app.dao.gemini_analyzer import get_gemini_analyzer
from uuid import uuid4

logger = logging.getLogger(__name__)

main_bp = Blueprint('main', __name__, template_folder='templates')

# In-memory stores below are intentionally per-process. They are documented in
# the module docstring; the brief notes here are reminders for anyone editing
# this file directly. See `create_app` for the MULTI_WORKER startup warning.

# Bridge between phone uploads and the browser tab waiting for them. Keys are
# short-lived tokens minted by the desktop page and embedded in the QR code.
MOBILE_UPLOAD_TTL = 900  # 15 minutes
_pending_mobile_uploads = {}
_mobile_upload_lock = threading.Lock()

# Single-use form tokens to block replay of state-changing POSTs that have
# already been processed (e.g. double-click submit).
FORM_TOKEN_TTL = 900  # 15 minutes
_used_form_tokens = {}
_form_token_lock = threading.Lock()

# `_rate_limit_events[(scope, identity)] = deque[timestamp]`. A deque per
# bucket avoids re-allocating a list every check and keeps trimming O(1) at
# the head.
_rate_limit_lock = threading.Lock()
_rate_limit_events = defaultdict(deque)


def _get_lan_ipv4() -> Optional[str]:
    """Best-effort primary LAN IPv4 for QR links when the dev server is opened via localhost."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.25)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return None


def _request_host_is_loopback() -> bool:
    host = (request.host or "").split(":")[0].lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    if host.startswith("127."):
        return True
    return False


def _public_base_url():
    """Base URL for QR / phone links. PUBLIC_BASE_URL wins (use for production, e.g. https://claritygrader.net)."""
    base = (os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if base:
        return base
    if _request_host_is_loopback():
        lan = _get_lan_ipv4()
        if lan:
            port = request.environ.get("SERVER_PORT", "5000")
            try:
                p = int(port)
            except ValueError:
                p = 5000
            return f"http://{lan}:{p}"
    return request.host_url.rstrip("/")


def _url_looks_like_loopback(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return "localhost" in u or "127.0.0.1" in u or "::1" in u


def _pending_put(token: str, class_id: str, user_id: str, upload_kind: str = "grades") -> None:
    with _mobile_upload_lock:
        _pending_mobile_uploads[token] = {
            "class_id": class_id,
            "user_id": user_id,
            "upload_kind": upload_kind,
            "created": time.time(),
            "file": None,
            "uploaded": False,
            "filename": None,
            "content_type": None,
        }


def _pending_get(token: str):
    with _mobile_upload_lock:
        p = _pending_mobile_uploads.get(token)
        if not p:
            return None
        if time.time() - p["created"] > MOBILE_UPLOAD_TTL:
            del _pending_mobile_uploads[token]
            return None
        return p


def _pending_delete(token: str) -> None:
    with _mobile_upload_lock:
        _pending_mobile_uploads.pop(token, None)


def _consume_one_time_form_token(namespace: str, token: str) -> bool:
    """Return True once per (namespace, token) within TTL; False for replays."""
    key = f"{namespace}:{token}"
    now = time.time()
    with _form_token_lock:
        expired = [
            k for k, created in _used_form_tokens.items()
            if now - created > FORM_TOKEN_TTL
        ]
        for k in expired:
            _used_form_tokens.pop(k, None)

        if key in _used_form_tokens:
            return False

        _used_form_tokens[key] = now
        return True

DEFAULT_REQUIRED_MS = 2

# Unicode Collation Algorithm collator — instantiated once at module load for
# performance. Used by all student-sorting functions to match Canvas ordering.
_uca_collator = pyuca.Collator()

# Shared caps for all bulk-import entry points (CSV and JSON body alike).
# Bytes cap matches the pre-existing LO CSV limit; row cap keeps a single
# request from blocking a worker on thousands of upserts / DoS-ing memory.
MAX_IMPORT_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ROWS = 2000


def _normalize_csv_key(key: Any) -> str:
    nk = str(key or "").strip().lower().lstrip("\ufeff")
    return re.sub(r"[^a-z0-9]+", "_", nk).strip("_")


def _csv_row_norm_keys(row: Dict[str, Any]) -> Dict[str, str]:
    """Lowercase + strip CSV header keys (handles UTF-8 BOM on first column)."""
    out: Dict[str, str] = {}
    for k, v in row.items():
        if k is None:
            continue
        nk = _normalize_csv_key(k)
        if isinstance(v, str):
            out[nk] = v.strip()
        elif v is None:
            out[nk] = ""
        else:
            out[nk] = str(v).strip()
    return out


def _csv_formula_safe(value: Any) -> str:
    """Neutralize CSV/spreadsheet formula injection (OWASP CSV injection).

    A name/title/description stored verbatim here could later be opened in
    Excel/Sheets (e.g. via a future export) and execute as a formula if it
    starts with =, +, -, or @. Prefixing with a single quote forces every
    major spreadsheet app to treat it as literal text; it's invisible in
    plain HTML/JSON contexts where the value is just displayed as-is.
    """
    s = str(value or "")
    if s and s[0] in ("=", "+", "-", "@"):
        return "'" + s
    return s


def parse_learning_objectives_csv_text(text: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Parse CSV for Canvas-style outcomes: required title; optional description and calculation_int.

    Initial required_ms is derived from calculation_int when valid (clamped 1–5); otherwise defaults.
    Instructors adjust Ms on the confirm-import UI, so calculation_int issues are handled silently.
    """
    warnings: List[str] = []
    text = (text or "").strip()
    if not text:
        return [], ["File is empty"]
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["Missing header row"]
    need = {"title"}
    lower_fn = {_normalize_csv_key(f) for f in reader.fieldnames if f}
    missing = need - lower_fn
    if missing:
        return [], [
            "CSV must include a title column (Canvas outcomes export). "
            f"Missing: {', '.join(sorted(missing))}"
        ]

    rows_out: List[Dict[str, Any]] = []
    for raw in reader:
        r = _csv_row_norm_keys(raw)
        title = (r.get("title") or "").strip()
        if not title:
            continue
        desc = (r.get("description") or "").strip()
        calc_raw = (r.get("calculation_int") or "").strip()
        required_ms = DEFAULT_REQUIRED_MS
        if calc_raw:
            try:
                ci = int(float(calc_raw))
                required_ms = max(1, min(5, ci))
            except (ValueError, TypeError):
                required_ms = DEFAULT_REQUIRED_MS
        rows_out.append(
            {
                "vendor_code": _csv_formula_safe(title),
                "required_ms": required_ms,
                "description": _csv_formula_safe(desc) or None,
            }
        )
    if not rows_out:
        return [], ["No data rows with a non-empty title"]
    return rows_out, warnings


def _normalize_spaces(value: str) -> str:
    return " ".join((value or "").strip().split())


def _student_name_from_csv_row(row: Dict[str, str]) -> str:
    raw = (
        row.get("full_name")
        or row.get("student_name")
        or row.get("name")
        or row.get("student")
        or ""
    ).strip()
    first = (row.get("first_name") or row.get("first") or "").strip()
    last = (row.get("last_name") or row.get("last") or "").strip()
    if not raw and (first or last):
        raw = f"{first} {last}".strip()
    raw = _normalize_spaces(raw)
    if not raw:
        return ""
    if "," in raw:
        left, right = raw.split(",", 1)
        last_name = _normalize_spaces(left)
        rest = _normalize_spaces(right)
        if rest and last_name:
            return f"{rest} {last_name}".strip()
        return rest or last_name
    return raw


def _student_email_key(email: str) -> str:
    return (email or "").strip().lower()


def _student_name_key(name: str) -> str:
    return _normalize_spaces((name or "").replace(",", " ").lower())


def _allowed_grade_upload_signature(file_bytes: bytes, filename: str) -> bool:
    fn = str(filename or "").strip().lower()
    if fn.endswith(".pdf"):
        return file_bytes.startswith(b"%PDF")
    if fn.endswith(".png"):
        return file_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    if fn.endswith(".jpg") or fn.endswith(".jpeg"):
        return file_bytes.startswith(b"\xff\xd8")
    return False


def _request_cache() -> Dict[str, Any]:
    """Per-request memoization bucket. Safe outside request context (returns module dict).

    Several helpers in this module (ownership checks, assignment LO lookups,
    auto-convert flag) are called many times in the same request. Caching on
    ``flask.g`` is bounded by the request lifetime, so there is no risk of
    serving stale rows across requests.

    The ``RuntimeError`` branch covers unit tests that call helpers directly
    without a request context. Those tests want deterministic behavior, so we
    expose a module-level dict and accept that it accumulates across tests
    (cleared by test setUp/tearDown if needed).
    """
    try:
        if not hasattr(g, "_clarity_cache"):
            g._clarity_cache = {}
        return g._clarity_cache  # type: ignore[no-any-return]
    except RuntimeError:
        global _no_request_cache
        try:
            _no_request_cache  # type: ignore[name-defined]
        except NameError:
            _no_request_cache = {}  # type: ignore[assignment]
        return _no_request_cache  # type: ignore[name-defined,return-value]


def _class_auto_convert_m_enabled(class_id: str) -> bool:
    cache = _request_cache()
    ck = ("_class_auto_convert_m_enabled", str(class_id))
    if ck in cache:
        return cache[ck]
    val = False
    try:
        resp = (
            supabase_admin.table("classes")
            .select("auto_convert_m")
            .eq("id", class_id)
            .limit(1)
            .execute()
        )
        if resp.data:
            val = bool(resp.data[0].get("auto_convert_m"))
    except Exception as e:
        logger.error("Error reading auto_convert_m for class %s: %s", class_id, e)
    cache[ck] = val
    return val


_HW_REQUIRED_FOR_MARK_ERROR = (
    "Enter a homework % for this student before saving a grade other than A (Absent)."
)


def parse_students_csv_text(text: str) -> Tuple[List[Dict[str, str]], List[str]]:
    warnings: List[str] = []
    text = (text or "").strip()
    if not text:
        return [], ["File is empty"]

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["Missing header row"]

    lower_fn = {_normalize_csv_key(f) for f in reader.fieldnames if f}
    has_name_col = any(h in lower_fn for h in ("full_name", "student_name", "name", "student"))
    has_first_last = ("first_name" in lower_fn and "last_name" in lower_fn)
    if not has_name_col and not has_first_last:
        return [], [
            "CSV must include either a name/full_name/student_name column, "
            "or first_name and last_name columns."
        ]

    rows: List[Dict[str, str]] = []
    for raw in reader:
        row = _csv_row_norm_keys(raw)
        full_name = _student_name_from_csv_row(row)
        if not full_name:
            continue
        email = _student_email_key(row.get("email") or row.get("student_email") or "")
        rows.append({"full_name": _csv_formula_safe(full_name), "email": email})

    if not rows:
        return [], ["No student rows found"]

    rows.sort(key=lambda r: _student_sort_key_last_name(r.get("full_name")))
    return rows, warnings


def _class_instructor_id(class_id: str) -> Optional[str]:
    cache = _request_cache()
    ck = ("_class_instructor_id", str(class_id))
    if ck in cache:
        return cache[ck]
    val: Optional[str] = None
    try:
        resp = supabase_admin.table("classes").select("instructor_id").eq("id", class_id).limit(1).execute()
        if resp.data:
            val = resp.data[0].get("instructor_id")
    except Exception as e:
        logger.error("_class_instructor_id: %s", e)
    cache[ck] = val
    return val


def _user_ids_equal(a: Any, b: Any) -> bool:
    """Compare auth user ids / FK ids from Supabase (string case and whitespace tolerant)."""
    if a is None or b is None:
        return False
    sa = str(a).strip().lower()
    sb = str(b).strip().lower()
    if sa == sb:
        return True
    return sa.replace("-", "") == sb.replace("-", "")


def build_lo_csv_mobile_upload_context(
    class_id: str, user_id: Optional[str], role: Optional[str]
) -> Dict[str, Any]:
    """Token + URL for learning-objective CSV upload from phone (class owner only)."""
    empty: Dict[str, Any] = {
        "lo_mobile_upload_token": "",
        "lo_mobile_upload_url": "",
        "lo_mobile_upload_url_is_loopback": False,
    }
    if not user_id or (role or "").strip().lower() != "instructor":
        return empty
    owner_id = _class_instructor_id(class_id)
    if not owner_id or not _user_ids_equal(owner_id, user_id):
        return empty
    token = str(uuid4())
    _pending_put(token, class_id, user_id, upload_kind="lo_outcomes")
    url = f"{_public_base_url()}/class/{class_id}/mobile-upload/{token}"
    return {
        "lo_mobile_upload_token": token,
        "lo_mobile_upload_url": url,
        "lo_mobile_upload_url_is_loopback": _url_looks_like_loopback(url),
    }


def _select_class_pool_learning_objectives(select_cols: str, class_id: str):
    """Select learning objectives for a class."""
    return (
        supabase_admin.table("learning_objectives")
        .select(select_cols)
        .eq("class_id", class_id)
        .execute()
    )


def _allowed_lo_ids_for_grading(class_id, assignment_id=None) -> set:
    """LO ids that may be graded.

    When an assignment_id is given, only LOs actually linked to that
    assignment via assignment_objectives are allowed. A crafted request
    for a class LO not linked to the assignment is silently dropped before
    any DB write, matching the scoping the CSV export helpers already apply.
    When no assignment_id is given (unscoped grade entry), the full class
    LO pool is returned.
    """
    if assignment_id:
        return {str(x) for x in (Course.get_assignment_lo_ids(assignment_id) or [])}
    return {str(x) for x in (Course.get_lo_ids_for_class(class_id) or [])}


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


def _is_lo_name_not_null_error(err: Exception) -> bool:
    msg = str(err or "").lower()
    return (
        'learning_objectives' in msg
        and 'null value in column "name"' in msg
        and 'not-null constraint' in msg
    )


def _insert_learning_objectives_compat(payload: Any):
    """Insert LOs, retrying with legacy name when old DB schema still requires it."""
    try:
        return supabase_admin.table("learning_objectives").insert(payload).execute()
    except Exception as e:
        if not _is_lo_name_not_null_error(e):
            raise
        rows = payload if isinstance(payload, list) else [payload]
        legacy_rows: List[Dict[str, Any]] = []
        for row in rows:
            r = dict(row or {})
            vc = (r.get("vendor_code") or "").strip()
            if not r.get("name"):
                r["name"] = vc
            legacy_rows.append(r)
        legacy_payload = legacy_rows if isinstance(payload, list) else legacy_rows[0]
        return supabase_admin.table("learning_objectives").insert(legacy_payload).execute()


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
            _insert_learning_objectives_compat(piece)
            inserted += len(piece)
        except Exception as e:
            err_id = str(uuid4())
            logger.error("[%s] LO batch insert failed: %s", err_id, e)
            errors.append(f"Import failed (ref {err_id}).")
            break
    return inserted, skipped, errors


def _merge_lo_row(lo_lookup, lo_id, embed):
    """Merge canonical LO metadata with an optional grade-row embed (fills gaps)."""
    base = dict(lo_lookup.get(lo_id) or {})
    emb = embed if isinstance(embed, dict) else {}
    for key in ("vendor_code", "description", "required_ms"):
        v = emb.get(key)
        if v is not None and v != "" and (key not in base or base.get(key) in (None, "")):
            base[key] = v
    return base


def _lo_display_title(lo_info):
    """Display objective identifier in UI lists."""
    vc = (lo_info.get("vendor_code") or "").strip()
    return vc or "Unknown LO"


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


def api_login_required(f):
    """Return 401 JSON if the user has no active session (for API routes)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({"success": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def api_instructor_required(f):
    """Return 401 JSON if the user is not a logged-in instructor."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"success": False, "error": "Unauthorized"}), 401
        role = (session.get("role") or "").strip().lower()
        if role != "instructor":
            return jsonify({"success": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def _client_ip() -> str:
    # ProxyFix (registered in app/__init__.py) already resolves the real
    # client IP into `remote_addr`, trusting exactly one hop (Railway's
    # edge). Reading X-Forwarded-For directly here would let a client set
    # their own value and have it trusted as-is, defeating this rate limiter.
    return (request.remote_addr or "unknown").strip()


def _rate_limit(key_prefix: str, limit: int, window_sec: int) -> bool:
    now = time.time()
    key = f"{key_prefix}:{_client_ip()}:{session.get('user_id', 'anon')}"
    with _rate_limit_lock:
        dq = _rate_limit_events[key]
        while dq and (now - dq[0]) > window_sec:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True


def rate_limited(key_prefix: str, limit: int, window_sec: int):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not _rate_limit(key_prefix, limit, window_sec):
                return jsonify({"success": False, "error": "Rate limit exceeded. Please retry shortly."}), 429
            return f(*args, **kwargs)
        return wrapped
    return decorator


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


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def organize_by_learning_objectives(students, learning_objectives):
    """Maps student grades to the relevant Learning Objectives for the UI."""
    lo_dict = {str(lo['id']): {
        'id': str(lo['id']),
        'name': (lo.get('name') or _lo_display_title(lo)),
        'students_with_2m': [],
        'students_with_1m': [],
        'students_with_0m': [],
        'total_students': len(students)
    } for lo in learning_objectives}

    for student in students:
        student_grades = student.get('grades', [])
        for grade in student_grades:
            lo_id = str(grade['learning_objective_id'])
            if lo_id in lo_dict:
                m_count = 0
                top = grade.get('top_score')
                sec = grade.get('second_score')

                if Grade.is_mastery_mark(top):
                    m_count += 1
                if Grade.is_mastery_mark(sec):
                    m_count += 1

                raw_fn = (student.get("full_name") or "").strip()
                student_data = {
                    "id": student["id"],
                    "full_name": raw_fn,
                    "name": student.get("name")
                    or _student_display_name(
                        {"id": student.get("id"), "full_name": student.get("full_name")}
                    ),
                    "top_score": top,
                    "second_score": sec,
                }

                if m_count == 2:
                    lo_dict[lo_id]['students_with_2m'].append(student_data)
                elif m_count == 1:
                    lo_dict[lo_id]['students_with_1m'].append(student_data)
                else:
                    lo_dict[lo_id]['students_with_0m'].append(student_data)

    for lo in lo_dict.values():
        for col in ("students_with_2m", "students_with_1m", "students_with_0m"):
            lo[col].sort(key=_student_row_sort_key)

    return list(lo_dict.values())


def ensure_profile_exists(user_id, full_name=None, role='instructor'):
    """
    Upserts a row in the profiles table for the given user_id.
    Prevents foreign key errors when inserting classes or other records
    that reference profiles.id.
    """
    data = {"id": user_id, "role": role}
    if full_name:
        data["full_name"] = full_name
    try:
        supabase_admin.table("profiles").upsert(data, on_conflict="id").execute()
    except Exception:
        logger.debug("Profile upsert skipped for %s — may already exist", user_id)


def normalize_profile(enrollment):
    prof = enrollment.get('profiles', {})
    if isinstance(prof, list):
        prof = prof[0] if prof else {}
    return prof or {}


def _format_name_last_first(raw: str) -> str:
    """Display roster-style as ``Lastname Firstname`` (single space, no comma).

    Assumes stored ``full_name`` is ``First ... Last`` or ``Last, First ...``.
    Single-token names are returned unchanged.
    """
    s = (raw or "").strip()
    if not s:
        return s
    if "," in s:
        left, right = s.split(",", 1)
        last = left.strip()
        rest = right.strip()
        if not last:
            return s
        if rest:
            return f"{last} {rest}".strip()
        return last
    parts = s.split()
    if len(parts) == 1:
        return parts[0]
    last = parts[-1]
    first = " ".join(parts[:-1])
    return f"{last} {first}".strip()


def _student_display_name(prof: Dict[str, Any]) -> str:
    """Label for UI; avoids showing raw UUID when full_name is missing or was corrupted."""
    pid = str(prof.get("id") or "").strip()
    fn = (prof.get("full_name") or "").strip()
    if not fn or (pid and fn == pid):
        return "Unnamed student"
    return _format_name_last_first(fn)


def _csv_format_hw_score(score) -> str:
    if score is None:
        return ""
    if score == -1 or score == -1.0:
        return "-1"
    try:
        return str(int(round(float(score))))
    except (TypeError, ValueError):
        return ""


def _gradesheet_letter_map_from_rows(grade_rows) -> Dict[Tuple[str, str], str]:
    letter_map: Dict[Tuple[str, str], str] = {}
    for g in grade_rows or []:
        sid = str(g.get("student_id") or "").strip()
        lo_id = str(g.get("learning_objective_id") or "").strip()
        letter = str(g.get("top_score") or "").strip()
        if sid and lo_id and letter:
            letter_map[(sid, lo_id)] = letter
    return letter_map


def _gradesheet_csv_data_rows(
    students: List[Dict[str, Any]],
    learning_objectives: List[Dict[str, Any]],
    hw_map: Dict[str, Any],
    letter_map: Dict[Tuple[str, str], str],
) -> List[List[str]]:
    rows: List[List[str]] = []
    for student in students:
        sid = str(student.get("id") or "").strip()
        name = _csv_formula_safe(student.get("name") or "")
        hw_cell = _csv_formula_safe(_csv_format_hw_score(hw_map.get(sid)))
        lo_cells = [
            _csv_formula_safe(letter_map.get((sid, str(lo.get("id") or "").strip()), ""))
            for lo in learning_objectives
        ]
        rows.append([name, hw_cell] + lo_cells)
    return rows


_BLANK_GRADESHEET_NOTE = (
    "This is a blank template. Grade columns are intentionally empty. "
    "Re-uploading this file will CLEAR any grades already entered for this assignment."
)


def _build_gradesheet_csv_text(
    assignment_name: str,
    date_value: str,
    vendor_codes: List[str],
    data_rows: List[List[str]],
    include_clear_warning: bool,
) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Assignment", _csv_formula_safe(assignment_name)])
    writer.writerow(["Date", date_value])
    if include_clear_warning:
        writer.writerow(["NOTE", _BLANK_GRADESHEET_NOTE])
    writer.writerow([])
    writer.writerow(["Student Name", "HW"] + vendor_codes)
    for data_row in data_rows:
        writer.writerow(data_row)
    return "\ufeff" + output.getvalue()


def _learning_objectives_for_assignment(class_id: str, assignment_id: str) -> List[Dict[str, Any]]:
    """Class LO pool filtered to rows in assignment_objectives for this assignment."""
    try:
        pool = Course.get_learning_objectives(class_id) or []
    except Exception as e:
        logger.error("gradesheet export: failed to load LOs for class %s: %s", class_id, e)
        pool = []
    linked = {
        str(lo_id).strip()
        for lo_id in (Course.get_assignment_lo_ids(assignment_id) or [])
        if str(lo_id).strip()
    }
    if not linked:
        return []
    return [
        lo for lo in pool
        if str(lo.get("id") or "").strip() in linked
    ]


def _gradesheet_export_bundle(class_id: str, assignment_id: str):
    """Assignment row, roster, LOs, HW map. None if the assignment is missing."""
    try:
        assignment_result = (
            supabase_admin.table("assignments")
            .select("id, name, date_returned")
            .eq("id", assignment_id)
            .eq("class_id", class_id)
            .limit(1)
            .execute()
        )
        assignment = (assignment_result.data or [None])[0]
    except Exception as e:
        logger.error("gradesheet export: failed to load assignment %s: %s", assignment_id, e)
        assignment = None
    if not assignment:
        return None

    class_data = Course.get_full_class_data(class_id)
    students, _, _ = _process_enrollments(class_data) if class_data else ([], [], {})
    learning_objectives = _learning_objectives_for_assignment(class_id, assignment_id)

    hw_map = Homework.get_hw_scores_map_for_assignment(class_id, assignment_id)
    return assignment, students, learning_objectives, hw_map


def _gradesheet_letter_map_for_assignment(
    assignment_id: str,
    students: List[Dict[str, Any]],
    learning_objectives: List[Dict[str, Any]],
) -> Dict[Tuple[str, str], str]:
    letter_map: Dict[Tuple[str, str], str] = {}
    lo_ids = [str(lo.get("id") or "").strip() for lo in learning_objectives if lo.get("id")]
    student_ids = [str(s.get("id") or "").strip() for s in students if s.get("id")]
    if not lo_ids or not student_ids:
        return letter_map
    try:
        grade_rows: List[Dict[str, Any]] = []
        chunk = 100
        for i in range(0, len(student_ids), chunk):
            batch = student_ids[i:i + chunk]
            gresp = (
                supabase_admin.table("grades")
                .select("student_id, learning_objective_id, top_score")
                .eq("assignment_id", assignment_id)
                .in_("student_id", batch)
                .in_("learning_objective_id", lo_ids)
                .execute()
            )
            grade_rows.extend(gresp.data or [])
        letter_map = _gradesheet_letter_map_from_rows(grade_rows)
    except Exception as e:
        logger.error(
            "gradesheet export: failed to load grades for assignment %s: %s",
            assignment_id, e,
        )
        letter_map = {}
    return letter_map


def _enrolled_import_name_index(profiles: List[Dict[str, Any]]) -> Dict[str, str]:
    """Map stored names and Last-First display names to enrolled student ids."""
    index: Dict[str, str] = {}

    def add(key: str, sid: str) -> None:
        raw = (key or "").strip()
        if not raw or not sid:
            return
        lower = raw.lower()
        if lower not in index:
            index[lower] = sid
        nk = _student_name_key(raw)
        if nk and nk not in index:
            index[nk] = sid

    for p in profiles:
        sid = str(p.get("id") or "").strip()
        fn = (p.get("full_name") or "").strip()
        if not sid or not fn:
            continue
        add(fn, sid)
        add(_format_name_last_first(fn), sid)
    return index


def _lookup_enrolled_import_student_id(
    csv_name: str, name_index: Dict[str, str]
) -> Optional[str]:
    n = (csv_name or "").strip()
    if not n:
        return None
    return name_index.get(n.lower()) or name_index.get(_student_name_key(n))


def _generate_sort_name(full_name: str) -> str:
    """Convert a stored ``'First Last[...]'`` name to ``'Last[...], First'`` form.

    This is the canonical heuristic for auto-generating a ``sort_name`` value
    from a student's stored ``full_name``.  It faithfully inverts the Canvas
    CSV import conversion (Canvas exports ``'Last, First'``; import stores
    ``'First Last'``), so Canvas-imported names regenerate their exact
    Canvas sort string automatically — including multi-word last names
    (``'Evelyn Juarez Salgado'`` → ``'Juarez Salgado, Evelyn'``), names with
    Von/Van prefixes (``'Zachary Von Huben'`` → ``'Von Huben, Zachary'``),
    suffixes treated as part of the last-name cluster
    (``'Emilio Benito Velasco Jr'`` → ``'Benito Velasco Jr, Emilio'``), and
    hyphen-with-space names preserved from import
    (``'Kaiya Smith- Pauley'`` → ``'Smith- Pauley, Kaiya'``).

    Rule: **the first whitespace token is the first name; everything after it
    is the last-name cluster.**

    Comma-format strings (already ``'Last, First'``) and single-token names
    are returned unchanged.
    """
    s = (full_name or "").strip()
    if not s:
        return s
    if "," in s:
        return s  # Already "Last, First" — pass through unchanged
    parts = s.split()
    if len(parts) == 1:
        return parts[0]
    first = parts[0]
    last_cluster = " ".join(parts[1:])
    return f"{last_cluster}, {first}"


def _student_sort_key_last_name(display_name: Optional[str]) -> Any:
    """UCA sort key for a student name string (full_name or display_name).

    Generates a ``'Last, First'`` sort_name via :func:`_generate_sort_name`,
    then applies the Unicode Collation Algorithm via :data:`_uca_collator`.
    This exactly matches Canvas roster ordering including hyphenated names,
    multi-word last names, Von/Van prefixes, and suffix clusters.

    Falls back to a high sentinel for blank names so unknown students sort last.
    """
    raw = (display_name or "").strip()
    if not raw:
        return _uca_collator.sort_key("\uffff")
    return _uca_collator.sort_key(_generate_sort_name(raw))


def _student_row_sort_key(student: Dict[str, Any]) -> Any:
    """UCA sort key for a student dict, preferring the stored ``sort_name``.

    When ``sort_name`` is populated in the DB (and present in the dict),
    it is used directly so any manual correction made via the edit UI takes
    effect immediately.  Falls back to generating sort_name on the fly from
    ``full_name`` or ``name`` using :func:`_generate_sort_name` so the
    function is safe to call on dicts that pre-date the column migration.
    """
    sort_name = (student.get("sort_name") or "").strip()
    if sort_name:
        return _uca_collator.sort_key(sort_name)
    label = (student.get("full_name") or student.get("name") or "").strip()
    return _uca_collator.sort_key(_generate_sort_name(label))


def _batch_get_free_passes(student_ids, class_id):
    """Batch-fetch passes_used for a list of students. Returns {student_id: passes_used}."""
    if not student_ids:
        return {}
    try:
        resp = supabase_admin.table("free_passes") \
            .select("student_id, passes_used") \
            .eq("class_id", class_id) \
            .in_("student_id", student_ids) \
            .execute()
        return {r['student_id']: r['passes_used'] for r in (resp.data or [])}
    except Exception:
        return {}


def _load_students_from_grades(class_id):
    """Return a list of students (with grades) by scanning grades for this class."""
    try:
        grades_result = supabase_admin.table("grades") \
            .select("student_id, learning_objective_id, top_score, second_score, learning_objectives(id, vendor_code, description, class_id, required_ms)") \
            .eq("learning_objectives.class_id", class_id) \
            .execute()
        grades = grades_result.data or []
    except Exception as e:
        logger.error("Error loading grades for class %s: %s", class_id, e)
        grades = []

    # Collect unique student IDs from grade rows
    students_by_id = {}
    for g in grades:
        lo = g.get('learning_objectives') or {}
        if lo.get('class_id') != class_id:
            continue
        student_id = g.get('student_id')
        if not student_id:
            continue
        if student_id not in students_by_id:
            students_by_id[student_id] = {'id': student_id, 'name': None, 'raw_grades': []}
        students_by_id[student_id]['raw_grades'].append(g)

    # Batch-fetch profile names (never write UUID into full_name — that corrupts profiles)
    if students_by_id:
        unique_ids = list(students_by_id.keys())
        try:
            try:
                profiles_resp = supabase_admin.table("profiles") \
                    .select("id, full_name, email, sort_name").in_("id", unique_ids).execute()
            except Exception:
                profiles_resp = supabase_admin.table("profiles") \
                    .select("id, full_name").in_("id", unique_ids).execute()
            for p in (profiles_resp.data or []):
                pid = p.get('id')
                if pid in students_by_id:
                    raw_fn = (p.get("full_name") or "").strip()
                    students_by_id[pid]["full_name"] = raw_fn
                    students_by_id[pid]["email"] = (p.get("email") or "").strip()
                    students_by_id[pid]["sort_name"] = (p.get("sort_name") or "").strip()
                    students_by_id[pid]["name"] = _student_display_name(
                        {"id": pid, "full_name": p.get("full_name")}
                    )
        except Exception:
            pass
        for row in students_by_id.values():
            pid = str(row.get('id') or '')
            nm = (row.get('name') or '').strip()
            if not nm or nm == pid:
                row['name'] = 'Unknown student'

    # Seed from canonical class LOs so names resolve even when grade embeds are missing.
    lo_lookup = {}
    try:
        seed = _select_class_pool_learning_objectives(
            "id, name, vendor_code, required_ms", class_id
        )
        for lo in (seed.data or []):
            lid = str(lo.get("id")) if lo.get("id") else None
            if lid:
                lo_lookup[lid] = lo
    except Exception as e:
        logger.error("Error seeding LO lookup for class %s: %s", class_id, e)

    pool_lo_ids = set(lo_lookup.keys())
    for g in grades:
        lo = g.get("learning_objectives") or {}
        raw_id = g.get("learning_objective_id")
        lo_id = str(raw_id) if raw_id is not None else None
        if not lo_id and lo.get("id"):
            lo_id = str(lo.get("id"))
        if lo_id and pool_lo_ids and lo_id not in pool_lo_ids:
            continue
        if lo_id and lo_id not in lo_lookup and lo.get("id"):
            lo_lookup[lo_id] = lo

    # Aggregate per-LO across assignments for each student
    for student in students_by_id.values():
        raw = [
            g for g in student.pop('raw_grades')
            if (
                not pool_lo_ids
                or str(g.get("learning_objective_id") or "") in pool_lo_ids
            )
        ]
        student['learning_objectives'] = _aggregate_lo_grades(raw, lo_lookup)

    out = list(students_by_id.values())
    out.sort(key=_student_row_sort_key)
    return out


def _aggregate_lo_grades(
    raw_grades,
    lo_lookup,
    mastery_row_allowed: Optional[Callable[[Dict[str, Any]], bool]] = None,
):
    """Aggregate grades per LO across assignments.

    With per-assignment grading, a student may have multiple grade rows for the
    same LO (one per assignment).  This helper groups them and counts total M's
    so the student detail page can show e.g. "2 / 2 Ms".
    """
    lo_grades = {}
    allowed_ids = set(lo_lookup.keys()) if isinstance(lo_lookup, dict) else None
    for g in (raw_grades or []):
        lo_id = str(g.get('learning_objective_id')) if g.get('learning_objective_id') else None
        if not lo_id:
            continue
        if allowed_ids is not None and len(allowed_ids) > 0 and lo_id not in allowed_ids:
            continue
        if lo_id not in lo_grades:
            lo_info = _merge_lo_row(lo_lookup, lo_id, g.get("learning_objectives"))
            lo_grades[lo_id] = {
                'learning_objective_id': lo_id,
                'name': _lo_display_title(lo_info),
                'vendor_code': (lo_info.get('vendor_code') or '').strip(),
                'required_ms': lo_info.get('required_ms') or DEFAULT_REQUIRED_MS,
                'm_count': 0,
                'mr_count': 0,
                'grades_list': [],
                'grades_meta': [],
            }
        top = g.get('top_score')
        # New persistent flag (scripts/add_grades_counts_for_mastery.sql) wins;
        # legacy rows missing the column default to True so existing M's still
        # count. Optional callback retained for compatibility (used by tests
        # and any caller layering extra constraints on top of the persisted
        # flag); it can only further restrict, never re-enable.
        row_counts_persistent = g.get('counts_for_mastery')
        if row_counts_persistent is None:
            row_counts_persistent = True
        row_counts_persistent = bool(row_counts_persistent)
        row_counts_callback = True
        if mastery_row_allowed is not None:
            try:
                row_counts_callback = bool(mastery_row_allowed(g))
            except Exception:
                row_counts_callback = True
        row_counts = row_counts_persistent and row_counts_callback
        if Grade.is_mastery_mark(top) and row_counts:
            lo_grades[lo_id]['m_count'] += 1
        if top == 'MR' and row_counts:
            lo_grades[lo_id]['mr_count'] += 1
        lo_grades[lo_id]['grades_list'].append(top)
        lo_grades[lo_id]['grades_meta'].append({
            'score': top,
            'counts_for_mastery': row_counts,
        })

    results = []
    for lo in lo_grades.values():
        lo['is_passed'] = lo['m_count'] >= lo['required_ms']
        lo['top_score'] = lo['grades_list'][0] if lo['grades_list'] else None
        lo['second_score'] = lo['grades_list'][1] if len(lo['grades_list']) > 1 else None
        # Mirror the same indices into grades_meta so templates can render a
        # red "non-counting" outline on each badge without a second lookup.
        meta = lo['grades_meta']
        lo['top_meta'] = meta[0] if meta else None
        lo['second_meta'] = meta[1] if len(meta) > 1 else None
        results.append(lo)
    return results


def _process_enrollments(class_data):
    """Extract students from enrollment data, aggregating grades per LO.

    Returns:
        (active_students, all_students, lo_lookup)
        - active_students: non-muted students with aggregated grades
        - all_students: all students (including muted) with aggregated grades
        - lo_lookup: {str(lo_id): lo_dict}
    """
    lo_lookup = {str(lo.get('id')): lo for lo in class_data.get('learning_objectives', [])}
    active_students = []
    all_students = []
    for e in class_data.get('enrollments', []):
        prof = normalize_profile(e)
        if not prof or not prof.get('id'):
            continue
        prof['learning_objectives'] = _aggregate_lo_grades(
            prof.get('grades', []) or [], lo_lookup
        )
        prof['name'] = _student_display_name(prof)
        prof['email'] = (prof.get('email') or '').strip()
        prof['muted'] = e.get('muted', False)
        all_students.append(prof)
        if not prof['muted']:
            active_students.append(prof)
    active_students.sort(key=_student_row_sort_key)
    all_students.sort(key=_student_row_sort_key)
    return active_students, all_students, lo_lookup


# We deliberately enumerate columns instead of `select("*")` so adding new
# columns later doesn't accidentally widen the payload for every page that
# loads assignments.
_ASSIGNMENT_COLS = (
    "id, class_id, name, homework_group, date_returned, revision_due, created_at, "
    "assignment_type, "
    "assignment_objectives!assignment_objectives_assignment_id_fkey("
    "learning_objective_id, "
    "learning_objectives(id, vendor_code, description))"
)


def load_assignments_for_class(class_id, desc=False):
    """Load assignments with linked LOs for a class.

    Centralizes the repeated assignment query used by multiple route handlers and
    selects only the columns templates/APIs read.

    Returns:
        list of assignment dicts (empty list on error).
    """
    try:
        assignments_result = (
            supabase_admin.table("assignments")
            .select(_ASSIGNMENT_COLS)
            .eq("class_id", class_id)
            .order("created_at", desc=desc)
            .execute()
        )
        return assignments_result.data or []
    except Exception as e:
        logger.error("Error loading assignments for class %s: %s", class_id, e)
        return []


# ============================================================================
# AUTHENTICATION ROUTES
# ============================================================================

@main_bp.route("/")
@main_bp.route("/login")
def login_page():
    return render_template("login.html")

@main_bp.route("/signup")
def signup_page():
    from config import Config
    return render_template("signup.html", invite_code_required=bool(Config.SIGNUP_INVITE_CODE))

@main_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for('main.login_page'))

@main_bp.route("/api/set-instructor-mode", methods=["POST"])
@api_login_required
def set_instructor_mode():
    data = request.get_json() or {}
    mode = data.get('mode')
    if mode not in ('mark', 'shelbi'):
        return jsonify({"success": False, "error": "Invalid mode"}), 400
    session['instructor_mode'] = mode
    return jsonify({"success": True, "mode": mode})

@main_bp.route("/api/login", methods=["POST"])
@rate_limited("api_login", limit=20, window_sec=300)
def login():
    data = request.get_json()
    try:
        result = supabase.auth.sign_in_with_password({
            "email": data.get("email"), "password": data.get("password")
        })
        if result.user:
            user_id = result.user.id
            metadata_role = result.user.user_metadata.get('role', 'student')

            # profiles.role is the DB-authoritative source for authorization
            # decorators (@api_instructor_required etc). Trusting the JWT's
            # embedded user_metadata directly would mean a user who can edit
            # their own Auth metadata (via the Supabase client SDK) could
            # self-promote. We only fall back to the metadata role when no
            # profile row exists yet (a brand-new account); once a profile
            # exists, its role is authoritative and is never overwritten by
            # metadata again (see ensure_profile_exists call below).
            existing_role = None
            try:
                prof_resp = (
                    supabase_admin.table("profiles")
                    .select("role")
                    .eq("id", user_id)
                    .limit(1)
                    .execute()
                )
                if prof_resp.data:
                    existing_role = (prof_resp.data[0].get("role") or "").strip() or None
            except Exception as e:
                logger.error("profiles.role lookup failed for %s: %s", user_id, e)

            actual_role = existing_role or metadata_role
            session.clear()
            session['user_id'] = user_id
            session['role'] = actual_role
            session['full_name'] = result.user.user_metadata.get('full_name', '')
            session['csrf_token'] = secrets.token_urlsafe(32)
            # Ensure profile exists on every login in case it was missed at signup.
            # `role` here only matters for the INSERT path (brand-new profile);
            # for an existing profile it re-affirms the same DB value read above.
            ensure_profile_exists(
                user_id,
                full_name=result.user.user_metadata.get('full_name'),
                role=actual_role
            )
            return jsonify({"success": True, "redirect": f"/{actual_role}/dashboard"})
        return jsonify({"success": False, "message": "Invalid credentials"})
    except Exception as e:
        return _safe_api_error("Invalid credentials", 401, log_detail=e)


@main_bp.route("/api/signup", methods=["POST"])
@rate_limited("api_signup", limit=10, window_sec=300)
def signup():
    data = request.get_json()
    try:
        from config import Config
        invite_code_env = (Config.SIGNUP_INVITE_CODE or "").strip()
        submitted = (data.get("invite_code") or "").strip()
        if invite_code_env:
            if not submitted or not hmac.compare_digest(submitted, invite_code_env):
                return jsonify({"success": False, "message": "Invalid invite code."}), 403

        signup_name = (data.get("name") or "").strip()
        if len(signup_name) > 255:
            return jsonify({"success": False, "message": "Name must be 255 characters or fewer."}), 400

        login_redirect_url = f"{_public_base_url()}/login"
        result = supabase.auth.sign_up({
            "email": data.get("email"),
            "password": data.get("password"),
            "options": {
                "email_redirect_to": login_redirect_url,
                "data": {
                    "full_name": data.get("name"),
                    "role": "instructor",
                    "invite_code": submitted
                }
            }
        })

        if result.user:
            # Registration succeeds, but account access is blocked until email confirmation.
            # Do not create an authenticated app session at signup time.
            session.clear()
            return jsonify({
                "success": True,
                "requires_email_confirmation": True,
                "message": "Account created. Please confirm your email before signing in.",
            })
        return jsonify({"success": False, "message": "Failed to create account. Please try again."})

    except Exception as e:
        return _safe_api_error("Signup failed. Please try again.", 400, log_detail=e)


# ============================================================================
# DASHBOARD ROUTES
# ============================================================================

@main_bp.route("/student/dashboard")
@login_required
def student_dashboard():
    data = Student.get_dashboard_data(session['user_id'])
    
    auto_convert_m = False
    class_name = None
    if data:
        # Get class settings for auto-convert
        enrollments = data.get('enrollments', [])
        class_id = None
        if enrollments:
            cls = enrollments[0].get('classes', {})
            if cls:
                auto_convert_m = cls.get('auto_convert_m', False)
                class_name = cls.get('name')
                class_id = cls.get('id')

        # Build lo_lookup from embedded learning_objectives on each grade
        raw_grades = data.get('grades', []) or []
        lo_lookup = {}
        for g in raw_grades:
            lo = g.get('learning_objectives') or {}
            lo_id = str(lo.get('id')) if lo.get('id') else None
            if lo_id and lo_id not in lo_lookup:
                lo_lookup[lo_id] = lo

        # Mastery counting now comes from the persisted counts_for_mastery flag
        # on each grade row (set at first-entry time in save_grades). No need
        # to re-fetch HW maps per assignment here.
        data['learning_objectives'] = _aggregate_lo_grades(raw_grades, lo_lookup)

    return render_template("student_view.html", student=data, auto_convert_m=auto_convert_m, class_name=class_name)

@main_bp.route("/instructor/dashboard")
@login_required
def instructor_dashboard():
    if session.get('role') != 'instructor':
        return redirect(url_for('main.login_page'))
    db_classes = Course.get_all_for_instructor(session['user_id'])
    create_class_token = str(uuid4())
    session['create_class_token'] = create_class_token
    copy_class_token = str(uuid4())
    session['copy_class_token'] = copy_class_token
    return render_template(
        "instructor_select_class.html",
        classes=db_classes,
        create_class_token=create_class_token,
        copy_class_token=copy_class_token,
    )

# ============================================================================
# CLASS MANAGEMENT ROUTES
# ============================================================================

@main_bp.route("/class/<class_id>")
@login_required
def class_detail(class_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))
    class_data = Course.get_full_class_data(class_id)
    if not class_data:
        logger.error("class_detail: get_full_class_data returned None for class_id=%s", class_id)
        return redirect(url_for('main.instructor_dashboard'))

    students_for_template, all_students_for_modal, _ = _process_enrollments(class_data)
    summary = organize_by_learning_objectives(students_for_template, class_data.get('learning_objectives', []))
    assignments = load_assignments_for_class(class_id)

    overdue_raw = Grade.get_overdue_revisions(class_id)
    student_name_map = {s['id']: s.get('name', 'Unknown') for s in students_for_template}
    overdue_revisions = []
    for rev in overdue_raw:
        if rev['student_id'] in student_name_map:
            rev['student_name'] = student_name_map[rev['student_id']]
            overdue_revisions.append(rev)

    try:
        all_los = Course.get_learning_objectives(class_id)
    except Exception as e:
        logger.error("Error loading LOs for dashboard %s: %s", class_id, e)
        all_los = []

    lo_ctx = build_lo_csv_mobile_upload_context(
        class_id, session.get("user_id"), session.get("role")
    )
    lo_ctx.setdefault("lo_mobile_upload_token", "")
    lo_ctx.setdefault("lo_mobile_upload_url", "")
    lo_ctx.setdefault("lo_mobile_upload_url_is_loopback", False)

    return render_template('class_detail.html', 
                            class_id=class_id, 
                            class_name=class_data.get('name'), 
                            students=students_for_template, 
                            all_students=all_students_for_modal,
                            learning_objectives=summary,
                            all_los=all_los,
                            assignments=assignments,
                            overdue_revisions=overdue_revisions,
                            auto_convert_m=class_data.get('auto_convert_m', False),
                            min_masteries=class_data.get('min_masteries', 2),
                            **lo_ctx)


@main_bp.route("/class/<class_id>/learning-objectives")
@login_required
def class_learning_objectives_summary(class_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))

    class_data = Course.get_full_class_data(class_id)
    if not class_data:
        logger.error("class_learning_objectives_summary: get_full_class_data returned None for class_id=%s", class_id)
        return redirect(url_for('main.instructor_dashboard'))

    students_for_template, _, _ = _process_enrollments(class_data)
    class_los = class_data.get('learning_objectives', []) or []
    total_students = len(students_for_template)

    summary_rows = []
    for lo in class_los:
        lo_id = str(lo.get("id") or "")
        passed_count = 0
        for student in students_for_template:
            student_los = student.get("learning_objectives", []) or []
            matched = next(
                (slo for slo in student_los if str(slo.get("learning_objective_id") or "") == lo_id),
                None
            )
            if matched and bool(matched.get("is_passed")):
                passed_count += 1
        summary_rows.append({
            "id": lo_id,
            "vendor_code": lo.get("vendor_code") or lo.get("name") or "Objective",
            "name": lo.get("name") or lo.get("vendor_code") or "Objective",
            "required_ms": lo.get("required_ms", 2),
            "passed_count": passed_count,
        })

    summary_rows.sort(key=lambda row: str(row.get("vendor_code") or "").lower())

    return render_template(
        "class_learning_objectives_summary.html",
        class_id=class_id,
        class_name=class_data.get("name"),
        learning_objectives=summary_rows,
        total_students=total_students,
    )

@main_bp.route("/class/<class_id>/add_student", methods=["POST"])
@api_instructor_required
def add_student(class_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    data = request.get_json()
    email = data.get('email', '').strip()
    name = data.get('name', '').strip()
    # Optional: instructor-provided sort_name from the Add Student form.
    # Falls back to auto-generation when absent or blank.
    sort_name_raw = (data.get('sort_name') or '').strip()
    sort_name = sort_name_raw if sort_name_raw else _generate_sort_name(name)

    if not email or not name:
        return jsonify({"success": False, "error": "Email and name are required"}), 400

    try:
        # Always create a new student profile with a unique UUID
        # (students don't log in; professors add them, so same-name students are different people)
        student_id = str(uuid4())
        insert_row = {
            "id": student_id,
            "full_name": name,
            "sort_name": sort_name,
            "role": "student",
            "email": email,
        }
        try:
            supabase_admin.table("profiles").insert(insert_row).execute()
        except Exception as ins_err:
            insert_row.pop("email", None)
            logger.warning(
                "profiles insert with email failed (%s); retrying without email. "
                "If this persists, run scripts/add_profiles_email.sql on the database.",
                ins_err,
            )
            supabase_admin.table("profiles").insert(insert_row).execute()

        # Check if already enrolled
        existing_enrollment = supabase_admin.table("enrollments").select("id").eq("class_id", class_id).eq("student_id", student_id).execute()
        
        if existing_enrollment.data:
            return jsonify({"success": False, "error": "Student is already enrolled in this class"}), 400
        
        # Add enrollment
        supabase_admin.table("enrollments").insert({
            "class_id": class_id,
            "student_id": student_id
        }).execute()
        
        return jsonify({"success": True})
    except Exception as e:
        return _safe_api_error("Could not add student", 500, log_detail=e)

def _log_grade_deletions(rows: List[Dict[str, Any]], class_id: str, changed_by: Optional[str]) -> None:
    """Best-effort audit log for grade rows about to be hard-deleted.

    Reads pre-delete values so grade_change_log retains history even though the
    grades table itself has no soft-delete. Must never raise — a missing/not-yet-
    migrated audit table or a logging failure should never block the actual delete.
    """
    if not rows:
        return
    log_rows = [
        {
            "student_id": r.get("student_id"),
            "class_id": class_id,
            "learning_objective_id": r.get("learning_objective_id"),
            "assignment_id": r.get("assignment_id"),
            "operation": "DELETE",
            "old_value": {
                "top_score": r.get("top_score"),
                "second_score": r.get("second_score"),
                "counts_for_mastery": r.get("counts_for_mastery"),
            },
            "new_value": None,
            "changed_by": changed_by,
        }
        for r in rows
    ]
    # Chunk large batches (e.g. delete_class on a big roster) so one insert
    # call doesn't hit request/payload limits, matching api_import_grades.
    chunk_size = 150
    for i in range(0, len(log_rows), chunk_size):
        chunk = log_rows[i:i + chunk_size]
        try:
            supabase_admin.table("grade_change_log").insert(chunk).execute()
        except Exception as e:
            logger.warning("grade_change_log insert skipped (table may not exist yet): %s", e)


def _log_grade_upserts(rows: List[Dict[str, Any]], class_id: str, changed_by: Optional[str]) -> None:
    """Best-effort audit log for grade rows that were just written (inserted or updated).

    Symmetric to _log_grade_deletions. Records operation="UPSERT" so the
    change log captures every score write, not just clears. Must never raise —
    a missing/not-yet-migrated audit table or a logging failure should never
    block an already-completed grade save.
    """
    if not rows:
        return
    log_rows = [
        {
            "student_id": r.get("student_id"),
            "class_id": class_id,
            "learning_objective_id": r.get("learning_objective_id"),
            "assignment_id": r.get("assignment_id"),
            "operation": "UPSERT",
            "old_value": None,
            "new_value": {
                "top_score": r.get("top_score"),
                "counts_for_mastery": r.get("counts_for_mastery"),
            },
            "changed_by": changed_by,
        }
        for r in rows
    ]
    chunk_size = 150
    for i in range(0, len(log_rows), chunk_size):
        chunk = log_rows[i:i + chunk_size]
        try:
            supabase_admin.table("grade_change_log").insert(chunk).execute()
        except Exception as e:
            logger.warning("grade_change_log upsert insert skipped (table may not exist yet): %s", e)


def _clear_grade_cells_for_assignment(
    class_id: str,
    assignment_id: Optional[str],
    pairs: List[Dict[str, str]],
    changed_by: Optional[str],
) -> int:
    """Delete existing grade rows for student+LO pairs on this assignment.

    Same path as SpeedGrader empty-cell saves: pre-fetch, audit-log, delete.
    Pairs with no matching row are a silent no-op.
    """
    if not pairs:
        return 0
    clear_sids = list({str(it["student_id"]) for it in pairs if it.get("student_id")})
    clear_lo_ids = list({str(it["lo_id"]) for it in pairs if it.get("lo_id")})
    clear_keys = {
        f"{it['student_id']}|{it['lo_id']}"
        for it in pairs
        if it.get("student_id") and it.get("lo_id")
    }
    if not clear_sids or not clear_lo_ids or not clear_keys:
        return 0
    cleared_rows: List[Dict[str, Any]] = []
    try:
        clear_q = supabase_admin.table("grades").select(
            "student_id, learning_objective_id, assignment_id, top_score, second_score, counts_for_mastery"
        ).in_("student_id", clear_sids).in_("learning_objective_id", clear_lo_ids)
        clear_q = (
            clear_q.eq("assignment_id", assignment_id)
            if assignment_id
            else clear_q.is_("assignment_id", "null")
        )
        clear_resp = clear_q.execute()
        for r in (clear_resp.data or []):
            k = f"{r.get('student_id')}|{r.get('learning_objective_id')}"
            if k in clear_keys:
                cleared_rows.append(r)
    except Exception as e:
        logger.error("clear_grade_cells pre-fetch failed: %s", e)
        return 0

    if not cleared_rows:
        return 0
    _log_grade_deletions(cleared_rows, class_id, changed_by)
    for r in cleared_rows:
        try:
            del_q = (
                supabase_admin.table("grades").delete()
                .eq("student_id", r["student_id"])
                .eq("learning_objective_id", r["learning_objective_id"])
            )
            del_q = (
                del_q.eq("assignment_id", assignment_id)
                if assignment_id
                else del_q.is_("assignment_id", "null")
            )
            del_q.execute()
        except Exception as e:
            logger.error(
                "failed to clear grade for student=%s lo=%s: %s",
                r.get("student_id"), r.get("learning_objective_id"), e,
            )
    return len(cleared_rows)


@main_bp.route("/class/<class_id>/students/<student_id>/delete", methods=["POST"])
@api_instructor_required
def delete_student_from_class(class_id, student_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    try:
        # Remove enrollment for this class only
        supabase_admin.table("enrollments").delete().eq("class_id", class_id).eq("student_id", student_id).execute()

        # Remove grades scoped to this class
        lo_ids = Course.get_all_lo_ids_for_class(class_id)
        if lo_ids:
            try:
                existing = supabase_admin.table("grades").select(
                    "student_id, learning_objective_id, assignment_id, top_score, second_score, counts_for_mastery"
                ).eq("student_id", student_id).in_("learning_objective_id", lo_ids).execute()
                _log_grade_deletions(existing.data or [], class_id, session['user_id'])
            except Exception as e:
                logger.warning("Could not audit-log grade deletions for student %s: %s", student_id, e)
            supabase_admin.table("grades").delete().eq("student_id", student_id).in_("learning_objective_id", lo_ids).execute()

        # Remove homework scores for this class
        supabase_admin.table("homework_scores").delete().eq("student_id", student_id).eq("class_id", class_id).execute()

        return jsonify({"success": True})
    except Exception as e:
        logger.error("Error deleting student %s from class %s: %s", student_id, class_id, e)
        return _safe_api_error("Could not delete student", 500, log_detail=e)


# Simple email format validator — not RFC-complete, matches what browsers accept
# for type="email" and is consistent with existing signup/add-student validation.
_SIMPLE_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


@main_bp.route("/api/class/<class_id>/students/<student_id>/update", methods=["POST"])
@api_instructor_required
def api_update_student(class_id, student_id):
    """Update a student's name, email, and/or sort_name.

    Ownership is verified two ways:
    1. The instructor must own the class (via _instructor_owns_class).
    2. The student must be enrolled in that class (via _student_enrolled_in_class),
       preventing an instructor from editing a stranger's profile using a
       student_id they guessed.

    sort_name is written exactly as provided — never regenerated from name —
    so a manually corrected sort_name is never silently overwritten.
    """
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    if not _student_enrolled_in_class(class_id, student_id):
        return jsonify({"success": False, "error": "Student not enrolled in this class"}), 403

    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    sort_name = (data.get("sort_name") or "").strip()

    if not name:
        return jsonify({"success": False, "error": "Name is required"}), 400
    if len(name) > 255:
        return jsonify({"success": False, "error": "Name must be 255 characters or fewer"}), 400
    if len(sort_name) > 255:
        return jsonify({"success": False, "error": "Sort name must be 255 characters or fewer"}), 400
    if email:
        if len(email) > 255:
            return jsonify({"success": False, "error": "Email must be 255 characters or fewer"}), 400
        if not _SIMPLE_EMAIL_RE.match(email):
            return jsonify({"success": False, "error": "Invalid email format"}), 400

    # Pre-check email uniqueness with a clear 409 before hitting the DB,
    # so the instructor gets an actionable message rather than a generic 500.
    # The DB-level unique index (when added before launch) acts as a backstop
    # for the theoretical race window; this check handles the UX.
    if email:
        try:
            conflict = (
                supabase_admin.table("profiles")
                .select("id")
                .eq("email", email)
                .neq("id", student_id)
                .limit(1)
                .execute()
            )
            if conflict.data:
                return jsonify({
                    "success": False,
                    "error": "That email is already associated with another student account.",
                }), 409
        except Exception as e:
            logger.warning("Email uniqueness pre-check failed for student %s: %s", student_id, e)
            # Non-fatal: proceed with the update and let the DB constraint
            # (or duplicate) surface rather than blocking the save entirely.

    try:
        update_payload: Dict[str, Any] = {
            "full_name": name,
            # Use exactly what the instructor typed; fall back to generating from
            # the new name only when sort_name was left blank (e.g. cleared).
            "sort_name": sort_name if sort_name else _generate_sort_name(name),
        }
        if email:
            update_payload["email"] = email

        supabase_admin.table("profiles").update(update_payload).eq("id", student_id).execute()
        return jsonify({"success": True})
    except Exception as e:
        return _safe_api_error("Could not update student", 500, log_detail=e)


@main_bp.route("/class/<class_id>/students")
@login_required
def class_students(class_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))
    class_data = Course.get_full_class_data(class_id)
    
    if not class_data:
        return redirect(url_for('main.instructor_dashboard'))

    students, _, _ = _process_enrollments(class_data)

    if not students:
        students = _load_students_from_grades(class_id)

    return render_template("class_students.html", 
                            class_id=class_id, 
                            class_name=class_data['name'], 
                            students=students)

@main_bp.route("/class/<class_id>/delete", methods=["POST"])
@api_instructor_required
def delete_class(class_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    try:
        lo_ids = Course.get_all_lo_ids_for_class(class_id)

        if lo_ids:
            supabase_admin.table("assignment_objectives").delete().in_("learning_objective_id", lo_ids).execute()
            try:
                existing = supabase_admin.table("grades").select(
                    "student_id, learning_objective_id, assignment_id, top_score, second_score, counts_for_mastery"
                ).in_("learning_objective_id", lo_ids).execute()
                _log_grade_deletions(existing.data or [], class_id, session['user_id'])
            except Exception as e:
                logger.warning("Could not audit-log grade deletions for class %s: %s", class_id, e)
            supabase_admin.table("grades").delete().in_("learning_objective_id", lo_ids).execute()
            supabase_admin.table("learning_objectives").delete().in_("id", lo_ids).execute()

        # Remove assignments, enrollments, and the class itself
        supabase_admin.table("assignments").delete().eq("class_id", class_id).execute()
        supabase_admin.table("enrollments").delete().eq("class_id", class_id).execute()
        supabase_admin.table("classes").delete().eq("id", class_id).execute()

        return redirect(url_for('main.instructor_dashboard'))
    except Exception as e:
        logger.error("Error deleting class %s: %s", class_id, e)
        return _safe_api_error("Could not delete class", 500, log_detail=e)


@main_bp.route("/class/<class_id>/copy", methods=["POST"])
@login_required
def copy_class(class_id):
    """Duplicate a class for the same instructor: same course settings and LOs; no students or assignments."""
    if session.get("role") != "instructor":
        return redirect(url_for("main.login_page"))

    if not _instructor_owns_class(class_id):
        return redirect(url_for("main.instructor_dashboard"))

    form_token = (request.form.get("copy_class_token") or "").strip()
    session_token = (session.get("copy_class_token") or "").strip()
    if not form_token or form_token != session_token:
        return redirect(url_for("main.instructor_dashboard"))
    if not _consume_one_time_form_token("copy_class", form_token):
        return redirect(url_for("main.instructor_dashboard"))
    session["copy_class_token"] = str(uuid4())

    new_name = (request.form.get("name") or "").strip()
    new_days = (request.form.get("days") or "").strip()
    if not new_name:
        return redirect(url_for("main.instructor_dashboard"))

    user_id = session["user_id"]

    try:
        src_resp = (
            supabase_admin.table("classes")
            .select("*")
            .eq("id", class_id)
            .eq("instructor_id", user_id)
            .execute()
        )
        if not src_resp.data:
            return redirect(url_for("main.instructor_dashboard"))
        source = src_resp.data[0]

        lo_resp = _select_class_pool_learning_objectives(
            "vendor_code, description, required_ms", class_id
        )
        los = lo_resp.data or []
        lo_specs = []
        for lo in los:
            vc = (lo.get("vendor_code") or "").strip()
            if not vc:
                continue
            spec = {
                "vendor_code": vc,
                "required_ms": int(lo.get("required_ms") or DEFAULT_REQUIRED_MS),
            }
            desc = lo.get("description")
            if desc is not None:
                spec["description"] = desc
            lo_specs.append(spec)

        new_class_data = {
            "name": new_name,
            "semester": source.get("semester") or "",
            "instructor_id": user_id,
        }
        optional_fields = {
            "days": new_days,
            "num_learning_objectives": len(lo_specs) if lo_specs else int(source.get("num_learning_objectives") or 0),
            "min_masteries": int(source.get("min_masteries") or 2),
            "is_online": bool(source.get("is_online")),
            "auto_convert_m": bool(source.get("auto_convert_m")),
        }
        if source.get("hw_passes_enabled") is not None:
            optional_fields["hw_passes_enabled"] = bool(source.get("hw_passes_enabled"))
        if source.get("hw_passes_allowed") is not None:
            optional_fields["hw_passes_allowed"] = int(source.get("hw_passes_allowed") or 2)

        def _insert_class_row(data: dict):
            return supabase_admin.table("classes").insert(data).execute()

        new_id = None
        try:
            full_data = {**new_class_data, **optional_fields}
            ins = _insert_class_row(full_data)
            if ins.data:
                new_id = ins.data[0].get("id")
        except Exception as col_err:
            if "PGRST204" in str(col_err) or "schema cache" in str(col_err):
                reduced = {**new_class_data, **optional_fields}
                for k in ("is_online", "hw_passes_enabled", "hw_passes_allowed", "auto_convert_m"):
                    reduced.pop(k, None)
                try:
                    ins = _insert_class_row(reduced)
                    if ins.data:
                        new_id = ins.data[0].get("id")
                except Exception as err2:
                    if "PGRST204" in str(err2) or "schema cache" in str(err2):
                        ins = _insert_class_row(new_class_data)
                        if ins.data:
                            new_id = ins.data[0].get("id")
                    else:
                        raise
            else:
                raise

        if not new_id:
            return redirect(url_for("main.instructor_dashboard"))

        if lo_specs:
            lo_rows = [{**spec, "class_id": new_id} for spec in lo_specs]
            chunk = 100
            for i in range(0, len(lo_rows), chunk):
                _insert_learning_objectives_compat(lo_rows[i : i + chunk])

        return redirect(url_for("main.instructor_dashboard"))
    except Exception as e:
        logger.error("Error copying class %s: %s", class_id, e)
        return "Failed to copy class.", 500


@main_bp.route("/class/<class_id>/students/<student_id>")
@login_required
def class_student_detail(class_id, student_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))
    class_data = Course.get_full_class_data(class_id)
    if not class_data:
        return redirect(url_for('main.instructor_dashboard'))

    lo_lookup = {str(lo.get('id')): lo for lo in class_data.get('learning_objectives', [])}

    student = None
    for e in class_data.get('enrollments', []):
        if e.get('muted', False):
            continue
        prof = normalize_profile(e)
        if str(prof.get('id') or '') != str(student_id):
            continue
        prof['learning_objectives'] = _aggregate_lo_grades(prof.get('grades', []), lo_lookup)
        prof['name'] = _student_display_name(prof)
        prof['email'] = (prof.get('email') or '').strip()
        student = prof
        break

    if not student:
        for s in _load_students_from_grades(class_id):
            if s.get('id') == student_id:
                student = s
                break

    if student and not (student.get('email') or '').strip():
        try:
            pe = (
                supabase_admin.table("profiles")
                .select("email")
                .eq("id", student_id)
                .limit(1)
                .execute()
            )
            if pe.data:
                student['email'] = (pe.data[0].get('email') or '').strip()
        except Exception:
            pass

    if not student:
        return redirect(url_for('main.class_students', class_id=class_id))

    return render_template(
        "class_student_detail.html",
        class_id=class_id,
        class_name=class_data.get("name"),
        student=student,
    )

@main_bp.route("/class/<class_id>/assignments")
@login_required
def class_assignments(class_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))
    class_data = Course.get_full_class_data(class_id)
    
    if not class_data:
        return redirect(url_for('main.instructor_dashboard'))

    assignments = load_assignments_for_class(class_id)

    try:
        all_los = Course.get_learning_objectives(class_id)
    except Exception as e:
        logger.error("Error loading LOs for class %s: %s", class_id, e)
        all_los = []

    lo_ctx = build_lo_csv_mobile_upload_context(
        class_id, session.get("user_id"), session.get("role")
    )
    lo_ctx.setdefault("lo_mobile_upload_token", "")
    lo_ctx.setdefault("lo_mobile_upload_url", "")
    lo_ctx.setdefault("lo_mobile_upload_url_is_loopback", False)

    return render_template(
        "class_assignments.html",
        class_id=class_id,
        class_name=class_data["name"],
        assignments=assignments,
        all_los=all_los,
        **lo_ctx,
    )

def _insert_assignment_objectives(ao_rows: List[Dict[str, Any]]) -> None:
    """Insert assignment_objectives rows."""
    if not ao_rows:
        return
    supabase_admin.table("assignment_objectives").insert(ao_rows).execute()


def _assignment_type_fields_from_payload(data: Dict[str, Any]) -> str:
    """Parse assignment_type from create/update body.

    Invalid / missing type falls back to 'mastery_opp'.
    'project' is a valid label and follows the same path as 'exam'.
    """
    raw = data.get("assignment_type") or "mastery_opp"
    if not isinstance(raw, str):
        raw = "mastery_opp"
    return raw.strip() if raw.strip() in ASSIGNMENT_TYPES else "mastery_opp"


@main_bp.route("/class/<class_id>/create_assignment", methods=["POST"])
@api_instructor_required
def create_assignment(class_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403

    data = request.get_json() or {}
    logger.debug("create_assignment payload: %s", data)

    # Validate required fields
    name = (data.get('name') or '').strip()
    homework_group = Homework.canonicalize_homework_group_for_class(class_id, data.get('homework_group'))

    if not name:
        return jsonify({"success": False, "error": "Assignment name is required."}), 400
    if len(name) > 255:
        return jsonify({"success": False, "error": "Assignment name must be 255 characters or fewer."}), 400
    if not homework_group:
        return jsonify({"success": False, "error": "Homework group is required."}), 400
    if len(homework_group) > 100:
        return jsonify({"success": False, "error": "Homework group must be 100 characters or fewer."}), 400

    assignment_type = _assignment_type_fields_from_payload(data)

    try:
        # Create new assignment
        result = supabase_admin.table("assignments").insert({
            "class_id": class_id,
            "name": name,
            "homework_group": homework_group,
            "date_returned": data.get('date_returned'),
            "revision_due": data.get('revision_due'),
            "assignment_type": assignment_type,
        }).execute()
        
        # Link selected LOs to this assignment (batch insert)
        if result.data:
            assignment_id = result.data[0]['id']
            class_lo_ids = set(Course.get_lo_ids_for_class(class_id) or [])
            ao_rows = []
            for lo_id in data.get('selected_los', []):
                if not lo_id or lo_id not in class_lo_ids:
                    continue
                ao_rows.append({
                    "assignment_id": assignment_id,
                    "learning_objective_id": lo_id,
                })
            if ao_rows:
                _insert_assignment_objectives(ao_rows)
            return jsonify({"success": True, "id": assignment_id})
        return jsonify({"success": False, "error": "Could not create assignment"}), 500
    except Exception as e:
        logger.error("create_assignment failed: %s", e)
        return _safe_api_error("Could not create assignment", 500, log_detail=e)

@main_bp.route("/class/<class_id>/assignments/<assignment_id>/update", methods=["POST", "PUT"])
@api_instructor_required
def update_assignment(class_id, assignment_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    if not _assignment_belongs_to_class(class_id, assignment_id):
        return jsonify({"success": False, "error": "Invalid assignment for class"}), 400

    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    homework_group = Homework.canonicalize_homework_group_for_class(class_id, data.get('homework_group'))
    if not name:
        return jsonify({"success": False, "error": "Assignment name is required."}), 400
    if len(name) > 255:
        return jsonify({"success": False, "error": "Assignment name must be 255 characters or fewer."}), 400
    if not homework_group:
        return jsonify({"success": False, "error": "Homework group is required."}), 400
    if len(homework_group) > 100:
        return jsonify({"success": False, "error": "Homework group must be 100 characters or fewer."}), 400

    assignment_type = _assignment_type_fields_from_payload(data)

    update_fields = {
        "name": name,
        "homework_group": homework_group,
        "date_returned": data.get('date_returned'),
        "revision_due": data.get('revision_due'),
        "assignment_type": assignment_type,
    }

    try:
        # Update assignment
        supabase_admin.table("assignments").update(update_fields).eq(
            "id", assignment_id
        ).eq("class_id", class_id).execute()
        
        # Update linked LOs — delete existing, batch insert new
        supabase_admin.table("assignment_objectives").delete().eq("assignment_id", assignment_id).execute()
        class_lo_ids = set(Course.get_lo_ids_for_class(class_id) or [])
        ao_rows = []
        for lo_id in data.get('selected_los', []):
            if not lo_id or lo_id not in class_lo_ids:
                continue
            ao_rows.append({
                "assignment_id": assignment_id,
                "learning_objective_id": lo_id,
            })
        if ao_rows:
            _insert_assignment_objectives(ao_rows)
        
        return jsonify({"success": True})
    except Exception as e:
        return _safe_api_error("Could not update assignment", 500, log_detail=e)

@main_bp.route("/class/<class_id>/delete_assignment/<assignment_id>", methods=["POST"])
@api_instructor_required
def delete_assignment(class_id, assignment_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403

    try:
        # Verify the assignment actually belongs to this class before touching anything.
        owner_check = (
            supabase_admin.table("assignments")
            .select("id")
            .eq("id", assignment_id)
            .eq("class_id", class_id)
            .limit(1)
            .execute()
        )
        if not owner_check.data:
            return jsonify({"success": False, "error": "Not found"}), 404

        try:
            existing = supabase_admin.table("grades").select(
                "student_id, learning_objective_id, assignment_id, top_score, "
                "second_score, counts_for_mastery"
            ).eq("assignment_id", assignment_id).execute()
            _log_grade_deletions(existing.data or [], class_id, session["user_id"])
        except Exception as e:
            logger.warning(
                "Could not audit-log grade deletions for assignment %s: %s",
                assignment_id, e,
            )

        supabase_admin.table("assignment_objectives").delete().eq(
            "assignment_id", assignment_id
        ).execute()
        supabase_admin.table("assignments").delete().eq(
            "id", assignment_id
        ).eq("class_id", class_id).execute()
        return jsonify({"success": True})
    except Exception as e:
        return _safe_api_error("Could not delete assignment", 500, log_detail=e)


@main_bp.route("/class/<class_id>/assignments/<assignment_id>/export-blank-csv")
@login_required
def export_blank_assignment_csv(class_id, assignment_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))
    if not _assignment_belongs_to_class(class_id, assignment_id):
        return redirect(url_for('main.class_assignments', class_id=class_id))

    bundle = _gradesheet_export_bundle(class_id, assignment_id)
    if not bundle:
        return redirect(url_for('main.class_assignments', class_id=class_id))
    assignment, students, learning_objectives, hw_map = bundle

    assignment_name = assignment.get('name') or 'Assignment'
    date_value = assignment.get('date_returned') or date.today().isoformat()
    vendor_codes = [_csv_formula_safe(lo.get('vendor_code') or '') for lo in learning_objectives]
    data_rows = _gradesheet_csv_data_rows(
        students, learning_objectives, hw_map, {}
    )
    csv_text = _build_gradesheet_csv_text(
        assignment_name, date_value, vendor_codes, data_rows, True
    )
    safe_stem = secure_filename(str(assignment_name)) or "assignment"
    filename = f"{safe_stem}_blank_gradesheet.csv"
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@main_bp.route("/class/<class_id>/assignments/<assignment_id>/export-csv")
@login_required
def export_assignment_csv(class_id, assignment_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))
    if not _assignment_belongs_to_class(class_id, assignment_id):
        return redirect(url_for('main.class_reports', class_id=class_id))

    bundle = _gradesheet_export_bundle(class_id, assignment_id)
    if not bundle:
        return redirect(url_for('main.class_reports', class_id=class_id))
    assignment, students, learning_objectives, hw_map = bundle

    assignment_name = assignment.get('name') or 'Assignment'
    date_value = assignment.get('date_returned') or date.today().isoformat()
    vendor_codes = [_csv_formula_safe(lo.get('vendor_code') or '') for lo in learning_objectives]
    letter_map = _gradesheet_letter_map_for_assignment(
        assignment_id, students, learning_objectives
    )
    data_rows = _gradesheet_csv_data_rows(
        students, learning_objectives, hw_map, letter_map
    )
    csv_text = _build_gradesheet_csv_text(
        assignment_name, date_value, vendor_codes, data_rows, False
    )
    safe_stem = secure_filename(str(assignment_name)) or "assignment"
    filename = f"{safe_stem}_gradesheet.csv"
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _strip_csv_formula_prefix(value: str) -> str:
    s = str(value or "").strip()
    if len(s) >= 2 and s[0] == "'" and s[1] in ("=", "+", "-", "@"):
        return s[1:]
    return s


def _csv_row_cells(row) -> List[str]:
    return [_strip_csv_formula_prefix(c) for c in (row or [])]


def _csv_row_is_blank(cells: List[str]) -> bool:
    return all(not c for c in cells)


def parse_blank_gradesheet_csv_text(
    text: str,
    class_vendor_codes: List[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Parse the Download Blank CSV gradesheet. Returns (payload, error).

    Every LO header is present on each student, with "" for an empty cell.
    Import treats "" as clear-if-a-row-exists, and a missing key as skip.
    """
    raw = (text or "").lstrip("\ufeff")
    if not raw.strip():
        return None, "CSV file is empty."

    rows = list(csv.reader(io.StringIO(raw)))
    if len(rows) < 4:
        return None, (
            "CSV does not match the gradesheet format. "
            "Expected Assignment, Date, optional note rows, a blank separator, "
            "then a Student Name,HW,<LO codes> header."
        )

    r0 = _csv_row_cells(rows[0])
    r1 = _csv_row_cells(rows[1])
    if len(r0) < 2 or r0[0].lower() != "assignment" or not r0[1]:
        return None, "Row 1 must be Assignment,<assignment name>."
    if len(r1) < 1 or r1[0].lower() != "date":
        return None, "Row 2 must be Date,<date>."

    # Metadata (Assignment, Date, NOTE, extra note lines) may grow. Do not
    # assume a fixed header index. Skip every non-blank row after Date until
    # the blank separator; the following row is the column header.
    sep_idx = 2
    while sep_idx < len(rows) and not _csv_row_is_blank(_csv_row_cells(rows[sep_idx])):
        sep_idx += 1
    if sep_idx >= len(rows):
        return None, "CSV is missing the blank separator row before the header."
    header_idx = sep_idx + 1
    if header_idx >= len(rows):
        return None, "CSV is missing the Student Name,HW header row."

    header = _csv_row_cells(rows[header_idx])
    while header and not header[-1]:
        header.pop()
    if (
        len(header) < 2
        or header[0].lower() != "student name"
        or header[1].lower() != "hw"
    ):
        return None, (
            "The header row must start with Student Name,HW followed by learning-objective codes."
        )

    vendor_by_lower = {}
    for code in class_vendor_codes:
        c = str(code or "").strip()
        if c:
            vendor_by_lower.setdefault(c.lower(), c)

    lo_headers: List[str] = []
    seen_lo = set()
    unknown: List[str] = []
    for col in header[2:]:
        if not col:
            continue
        canonical = vendor_by_lower.get(col.lower())
        if not canonical:
            unknown.append(col)
            continue
        key = canonical.lower()
        if key in seen_lo:
            return None, f'Duplicate learning-objective column "{canonical}".'
        seen_lo.add(key)
        lo_headers.append(canonical)
    if unknown:
        shown = ", ".join(unknown[:8])
        extra = f" (and {len(unknown) - 8} more)" if len(unknown) > 8 else ""
        return None, (
            "CSV learning-objective headers must match this class's vendor codes. "
            f"Unknown: {shown}{extra}."
        )

    students: List[Dict[str, Any]] = []
    for raw_row in rows[header_idx + 1:]:
        cells = _csv_row_cells(raw_row)
        if _csv_row_is_blank(cells):
            continue
        name = cells[0] if cells else ""
        if not name:
            continue
        hw_raw = cells[1] if len(cells) > 1 else ""
        homework_pct = None
        if hw_raw:
            parsed_hw = Homework.parse_import_hw_pct(hw_raw)
            homework_pct = str(parsed_hw) if parsed_hw is not None else hw_raw

        grades: Dict[str, str] = {}
        for i, lo_code in enumerate(lo_headers):
            idx = i + 2
            mark = cells[idx] if idx < len(cells) else ""
            grades[lo_code] = mark.upper() if mark else ""

        students.append({
            "name": name,
            "grades": grades,
            "homework_pct": homework_pct,
        })
        if len(students) > MAX_IMPORT_ROWS:
            return None, f"CSV has too many student rows (max {MAX_IMPORT_ROWS})."

    return {
        "assignment_name": r0[1],
        "date_value": r1[1] if len(r1) > 1 else "",
        "learning_objectives": lo_headers,
        "homework_column": "HW",
        "students": students,
        "extraction_path": "csv",
    }, None


def _instructor_owns_class(class_id: str) -> bool:
    uid = session.get("user_id")
    owner = _class_instructor_id(class_id)
    return bool(uid and owner and _user_ids_equal(owner, uid))


def _assignment_belongs_to_class(class_id: str, assignment_id: str) -> bool:
    cache = _request_cache()
    ck = ("_assignment_belongs_to_class", str(class_id), str(assignment_id))
    if ck in cache:
        return cache[ck]
    val = False
    try:
        r = (
            supabase_admin.table("assignments")
            .select("id")
            .eq("id", assignment_id)
            .eq("class_id", class_id)
            .limit(1)
            .execute()
        )
        val = bool(r.data)
    except Exception:
        val = False
    cache[ck] = val
    return val


def _enrolled_student_ids_for_class(class_id: str) -> set:
    """Set of student_ids with an enrollment row in class_id."""
    cache = _request_cache()
    ck = ("_enrolled_student_ids_for_class", str(class_id))
    if ck in cache:
        return cache[ck]
    ids: set = set()
    try:
        resp = (
            supabase_admin.table("enrollments")
            .select("student_id")
            .eq("class_id", class_id)
            .execute()
        )
        ids = {str(r["student_id"]) for r in (resp.data or []) if r.get("student_id")}
    except Exception as e:
        logger.error("enrollment lookup failed for class %s: %s", class_id, e)
    cache[ck] = ids
    return ids


def _student_enrolled_in_class(class_id: str, student_id: str) -> bool:
    """True only if student_id has a real enrollment row in class_id.

    Fails closed (returns False) on lookup error or missing ids — grade/HW/
    exam/pass writes must never succeed for a student the DB can't confirm
    is actually on this class's roster.
    """
    if not class_id or not student_id:
        return False
    return str(student_id) in _enrolled_student_ids_for_class(class_id)


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


@main_bp.route("/class/<class_id>/delete_lo/<lo_id>", methods=["POST"])
@api_instructor_required
def delete_lo(class_id, lo_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    try:
        chk = (
            supabase_admin.table("learning_objectives")
            .select("id")
            .eq("id", lo_id)
            .eq("class_id", class_id)
            .limit(1)
            .execute()
        )
        if not chk.data:
            return jsonify({"success": False, "error": "Learning objective not found"}), 404
        # First delete assignment_objectives links
        supabase_admin.table("assignment_objectives").delete().eq("learning_objective_id", lo_id).execute()
        # Then delete grades
        try:
            existing = supabase_admin.table("grades").select(
                "student_id, learning_objective_id, assignment_id, top_score, second_score, counts_for_mastery"
            ).eq("learning_objective_id", lo_id).execute()
            _log_grade_deletions(existing.data or [], class_id, session['user_id'])
        except Exception as e:
            logger.warning("Could not audit-log grade deletions for lo %s: %s", lo_id, e)
        supabase_admin.table("grades").delete().eq("learning_objective_id", lo_id).execute()
        # Then delete the LO
        supabase_admin.table("learning_objectives").delete().eq("id", lo_id).eq("class_id", class_id).execute()
        return jsonify({"success": True})
    except Exception as e:
        logger.exception("delete_lo failed class_id=%s lo_id=%s", class_id, lo_id)
        return _safe_api_error("Could not delete learning objective", 500, log_detail=e)


@main_bp.route("/api/class/<class_id>/update-lo/<lo_id>", methods=["POST"])
@api_instructor_required
def api_update_learning_objective(class_id, lo_id):
    """Update display fields on an existing LO (same row id — grades and links stay intact)."""
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    data, err = _require_json_object()
    if err:
        return err[0], err[1]
    try:
        cur = (
            supabase_admin.table("learning_objectives")
            .select("id, vendor_code, description, required_ms")
            .eq("id", lo_id)
            .eq("class_id", class_id)
            .limit(1)
            .execute()
        )
        if not cur.data:
            return jsonify({"success": False, "error": "Learning objective not found"}), 404
        row = cur.data[0]
        vendor_raw = data.get("vendor_code")
        if vendor_raw is None:
            vendor_code = (row.get("vendor_code") or "").strip() or None
        else:
            vendor_code = str(vendor_raw).strip() or None
        if not vendor_code:
            return jsonify({"success": False, "error": "Objective code is required"}), 400
        if len(vendor_code) > 50:
            return jsonify({"success": False, "error": "Objective code must be 50 characters or fewer."}), 400
        if vendor_code and _lo_vendor_code_conflict(class_id, vendor_code, lo_id):
            return jsonify(
                {"success": False, "error": "Another objective in this class already uses that code."}
            ), 400
        desc_raw = data.get("description")
        if desc_raw is None:
            description = row.get("description")
        else:
            description = str(desc_raw).strip() or None
        if description and len(description) > 2000:
            return jsonify({"success": False, "error": "Description must be 2000 characters or fewer."}), 400
        if data.get("required_ms") is None:
            req = int(row.get("required_ms") or DEFAULT_REQUIRED_MS)
        else:
            try:
                req = int(data.get("required_ms"))
            except (TypeError, ValueError):
                req = DEFAULT_REQUIRED_MS
        req = max(1, min(5, req))
        supabase_admin.table("learning_objectives").update(
            {
                "vendor_code": vendor_code,
                "description": description,
                "required_ms": req,
            }
        ).eq("id", lo_id).eq("class_id", class_id).execute()
        return jsonify({"success": True})
    except Exception as e:
        logger.exception("api_update_learning_objective failed class_id=%s lo_id=%s", class_id, lo_id)
        return _safe_api_error("Could not update learning objective", 500, log_detail=e)


def _parse_lo_csv_upload() -> Tuple[Optional[str], Optional[List[Dict[str, Any]]], List[str]]:
    """Read CSV from request.files['file']; return (error_message or None, rows or None, warnings)."""
    f = request.files.get("file")
    if not f or not f.filename:
        return "No file uploaded", None, []
    if not f.filename.lower().endswith(".csv"):
        return "Upload a .csv file", None, []
    try:
        raw = f.read()
        if len(raw) > MAX_IMPORT_UPLOAD_BYTES:
            return "CSV must be 5MB or smaller", None, []
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return "CSV must be UTF-8 encoded", None, []
    rows, parse_warnings = parse_learning_objectives_csv_text(text)
    if not rows:
        return parse_warnings[0] if parse_warnings else "No objectives to import", None, parse_warnings
    if len(rows) > MAX_IMPORT_ROWS:
        return f"CSV has too many rows (max {MAX_IMPORT_ROWS})", None, parse_warnings
    return None, rows, parse_warnings


def _parse_student_csv_upload() -> Tuple[Optional[str], Optional[List[Dict[str, str]]], List[str]]:
    """Read CSV from request.files['file']; return (error_message or None, rows or None, warnings)."""
    f = request.files.get("file")
    if not f or not (f.filename or "").strip():
        return "Please choose a CSV file.", None, []
    if not f.filename.lower().endswith(".csv"):
        return "Upload a .csv file", None, []
    try:
        raw = f.read()
        if len(raw) > MAX_IMPORT_UPLOAD_BYTES:
            return "CSV must be 5MB or smaller", None, []
        text = raw.decode("utf-8-sig", errors="replace")
    except Exception:
        return "Could not read CSV file", None, []

    rows, parse_warnings = parse_students_csv_text(text)
    if not rows:
        return (
            parse_warnings[0] if parse_warnings else "No valid student rows found",
            None,
            parse_warnings,
        )
    if len(rows) > MAX_IMPORT_ROWS:
        return f"CSV has too many rows (max {MAX_IMPORT_ROWS})", None, parse_warnings
    return None, rows, parse_warnings


@main_bp.route("/api/class/<class_id>/preview-learning-objectives", methods=["POST"])
@api_instructor_required
def api_preview_learning_objectives(class_id):
    """Parse LO CSV; return rows for confirm UI (Ms edited client-side)."""
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403

    err, rows, parse_warnings = _parse_lo_csv_upload()
    if err:
        return jsonify({"success": False, "error": err, "warnings": parse_warnings}), 400

    preview_rows = annotate_learning_objective_rows_for_preview(class_id, rows or [])
    return jsonify(
        {
            "success": True,
            "rows": preview_rows,
            "warnings": parse_warnings,
            "count": len(preview_rows),
        }
    )


@main_bp.route("/api/class/<class_id>/import-learning-objectives", methods=["POST"])
@api_instructor_required
def api_import_learning_objectives(class_id):
    """Commit LO rows after user confirms (JSON body: { \"rows\": [...] })."""
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403

    data, err = _require_json_object()
    if err:
        return err[0], err[1]
    raw_rows = data.get("rows")
    if not isinstance(raw_rows, list) or len(raw_rows) == 0:
        return jsonify({"success": False, "error": "Missing or empty rows array"}), 400
    if len(raw_rows) > MAX_IMPORT_ROWS:
        return jsonify({"success": False, "error": f"Too many rows (max {MAX_IMPORT_ROWS})"}), 400

    rows: List[Dict[str, Any]] = []
    for item in raw_rows:
        if not isinstance(item, dict):
            continue
        vc = (item.get("vendor_code") or "").strip()
        if not vc:
            continue
        try:
            req = int(item.get("required_ms", DEFAULT_REQUIRED_MS))
        except (TypeError, ValueError):
            req = DEFAULT_REQUIRED_MS
        req = max(1, min(5, req))
        desc_raw = item.get("description")
        description = str(desc_raw).strip() if desc_raw is not None else ""
        rows.append(
            {
                "vendor_code": vc,
                "required_ms": req,
                "description": description or None,
            }
        )

    if not rows:
        return jsonify({"success": False, "error": "No valid rows to import"}), 400

    inserted, skipped, errors = import_learning_objectives_rows(class_id, rows)
    if errors and inserted == 0:
        return jsonify(
            {
                "success": False,
                "error": errors[0],
                "inserted": 0,
                "skipped": skipped,
            }
        ), 500

    return jsonify(
        {
            "success": True,
            "inserted": inserted,
            "skipped": skipped,
            "errors": errors,
        }
    )


@main_bp.route("/class/<class_id>/reports")
@login_required
def class_reports(class_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))
    class_data = Course.get_full_class_data(class_id)
    
    if not class_data:
        return redirect(url_for('main.instructor_dashboard'))

    students, _, _ = _process_enrollments(class_data)
    learning_objectives = class_data.get('learning_objectives', [])

    if not students:
        students = _load_students_from_grades(class_id)

    # Load assignments with their linked LOs and dates
    assignments = load_assignments_for_class(class_id, desc=False)

    return render_template(
        "class_reports.html",
        class_id=class_id,
        class_name=class_data["name"],
        students=students,
        learning_objectives=learning_objectives,
        assignments=assignments,
        auto_convert_m=class_data.get("auto_convert_m", False),
    )


def _send_via_resend(to_email: str, subject: str, body_text: str) -> Tuple[bool, str]:
    api_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    from_email = (os.environ.get("REPORTS_FROM_EMAIL") or "").strip()
    if not api_key:
        logger.error("Email send blocked: RESEND_API_KEY is not configured")
        return False, "Email is not configured."
    if not from_email:
        logger.error("Email send blocked: REPORTS_FROM_EMAIL is not configured")
        return False, "Email is not configured."

    payload = {
        "from": from_email,
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
        if resp.status_code >= 400:
            logger.error("Email provider rejected request (%s): %s", resp.status_code, resp.text[:400])
            return False, "Email provider rejected request."
        return True, ""
    except Exception as e:
        logger.error("Email send failed: %s", e)
        return False, "Email send failed."


@main_bp.route("/api/class/<class_id>/student/<student_id>/send-report-email", methods=["POST"])
@api_instructor_required
@rate_limited("email_single_report", limit=60, window_sec=3600)
def api_send_single_report_email(class_id, student_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403

    data, err = _require_json_object()
    if err:
        return err[0], err[1]
    subject = str(data.get("subject") or "").strip()
    body = str(data.get("body") or "").strip()
    if not subject or not body:
        return jsonify({"success": False, "error": "Missing subject or body"}), 400
    if len(subject) > 255:
        return jsonify({"success": False, "error": "Subject must be 255 characters or fewer."}), 400
    if len(body) > 10000:
        return jsonify({"success": False, "error": "Body must be 10,000 characters or fewer."}), 400

    try:
        enrollment = (
            supabase_admin.table("enrollments")
            .select("student_id, profiles(id, full_name, email)")
            .eq("class_id", class_id)
            .eq("student_id", student_id)
            .limit(1)
            .execute()
        )
        if not enrollment.data:
            return jsonify({"success": False, "error": "Student is not enrolled in this class"}), 404
        profile = normalize_profile({"profiles": enrollment.data[0].get("profiles")})
        to_email = (profile.get("email") or "").strip()
        if not to_email:
            return jsonify({"success": False, "error": "No email address is saved for this student"}), 400
    except Exception as e:
        logger.error("Error loading enrollment for report email: %s", e)
        return jsonify({"success": False, "error": "Could not load student email"}), 500

    ok, err = _send_via_resend(to_email, subject, body)
    if not ok:
        return jsonify({"success": False, "error": err}), 502
    return jsonify({"success": True, "to": to_email})


@main_bp.route("/api/class/<class_id>/send-report-emails", methods=["POST"])
@api_instructor_required
@rate_limited("email_bulk_report", limit=20, window_sec=3600)
def api_send_bulk_report_emails(class_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403

    data, err = _require_json_object()
    if err:
        return err[0], err[1]
    reports = data.get("reports")
    if not isinstance(reports, list) or not reports:
        return jsonify({"success": False, "error": "Missing reports payload"}), 400
    if len(reports) > 250:
        return jsonify({"success": False, "error": "Too many reports in one request"}), 400

    student_ids: List[str] = []
    for item in reports:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("student_id") or "").strip()
        if sid:
            student_ids.append(sid)
    if not student_ids:
        return jsonify({"success": False, "error": "No valid student ids provided"}), 400

    try:
        enrollments = (
            supabase_admin.table("enrollments")
            .select("student_id, profiles(id, full_name, email)")
            .eq("class_id", class_id)
            .in_("student_id", list(set(student_ids)))
            .execute()
        )
    except Exception as e:
        logger.error("Error loading enrollments for bulk report emails: %s", e)
        return jsonify({"success": False, "error": "Could not load student emails"}), 500

    email_by_student: Dict[str, str] = {}
    for row in (enrollments.data or []):
        sid = str(row.get("student_id") or "").strip()
        profile = normalize_profile({"profiles": row.get("profiles")})
        email = (profile.get("email") or "").strip()
        if sid and email:
            email_by_student[sid] = email

    sent = 0
    skipped = 0
    failures: List[str] = []
    for item in reports:
        if not isinstance(item, dict):
            skipped += 1
            continue
        sid = str(item.get("student_id") or "").strip()
        subject = str(item.get("subject") or "").strip()
        body = str(item.get("body") or "").strip()
        student_name = str(item.get("student_name") or sid).strip() or sid
        if not sid or not subject or not body:
            skipped += 1
            continue
        if len(subject) > 255 or len(body) > 10000:
            skipped += 1
            failures.append(f"{student_name}: email content too long")
            continue
        to_email = email_by_student.get(sid, "")
        if not to_email:
            skipped += 1
            failures.append(f"{student_name}: no saved email")
            continue
        ok, err = _send_via_resend(to_email, subject, body)
        if ok:
            sent += 1
        else:
            failures.append(f"{student_name}: {err}")

    if sent == 0 and failures:
        return jsonify({
            "success": False,
            "error": "No emails were sent.",
            "sent": 0,
            "skipped": skipped,
            "failures": failures[:20],
        }), 502

    return jsonify({
        "success": True,
        "sent": sent,
        "skipped": skipped,
        "failures": failures[:20],
    })

@main_bp.route("/class/<class_id>/student/<student_id>/history")
@login_required
def student_history(class_id, student_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))
    class_data = Course.get_full_class_data(class_id)
    if not class_data:
        return redirect(url_for('main.instructor_dashboard'))

    # Get student enrollment and profile info
    enrollment_data = None
    try:
        enrollment = supabase_admin.table("enrollments").select(
            "student_id, profiles(id, full_name, email)"
        ).eq("class_id", class_id).eq("student_id", student_id).single().execute()
        enrollment_data = enrollment.data
    except Exception:
        try:
            enrollment = supabase_admin.table("enrollments").select(
                "student_id, profiles(id, full_name)"
            ).eq("class_id", class_id).eq("student_id", student_id).single().execute()
            enrollment_data = enrollment.data
        except Exception as e:
            logger.error("Error loading student enrollment: %s", e)
            return redirect(url_for('main.class_reports', class_id=class_id))

    if not enrollment_data:
        return redirect(url_for('main.class_reports', class_id=class_id))

    profile = normalize_profile({'profiles': enrollment_data.get('profiles')})
    student_name = _student_display_name(profile) if profile.get('id') else 'Unknown student'
    student_email = (profile.get("email") or "").strip()
    profile_id = profile.get('id')

    # Get all grades for this student with assignment names
    try:
        grades_resp = supabase_admin.table("grades").select(
            "*, assignments(name)"
        ).eq("student_id", profile_id).execute()
        all_grades = grades_resp.data or []
    except Exception as e:
        logger.error("Error loading grades: %s", e)
        all_grades = []
    
    # Get all learning objectives for the class
    learning_objectives = class_data.get('learning_objectives', [])
    
    # Organize grades by learning objective
    student_grades = {}
    for grade in all_grades:
        lo_id = str(grade.get('learning_objective_id')) if grade.get('learning_objective_id') is not None else None
        if lo_id not in student_grades:
            student_grades[lo_id] = []
        student_grades[lo_id].append(grade)

    # Build grade data for template
    lo_grade_data = []
    for lo in learning_objectives:
        lo_id = str(lo.get('id'))
        grades = student_grades.get(lo_id, [])
        lo_info = {
            'id': lo_id,
            'vendor_code': lo.get('vendor_code'),
            'description': lo.get('description'),
            'grades': grades
        }
        lo_grade_data.append(lo_info)

    return render_template(
        "student_history.html",
        class_id=class_id,
        class_name=class_data['name'],
        student_id=student_id,
        student_name=student_name,
        student_email=student_email,
        learning_objectives=lo_grade_data,
    )

@main_bp.route("/class/<class_id>/speed_grader", endpoint='class_speed_grader')
@login_required
def class_speed_grader(class_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))
    class_data = Course.get_full_class_data(class_id)

    if not class_data:
        return redirect(url_for('main.instructor_dashboard'))

    hw_passes_allowed = 2

    # Load assignments with their linked LOs
    assignments = load_assignments_for_class(class_id)

    raw_enrollments = class_data.get('enrollments', [])
    lo_lookup = {str(lo.get('id')): lo for lo in class_data.get('learning_objectives', [])}
    students = []
    for enrollment in raw_enrollments:
        prof = normalize_profile(enrollment)
        if not prof.get('id'):
            continue
        prof['name'] = _student_display_name(prof)
        if enrollment.get('muted', False):
            continue
        students.append(prof)

    if not students:
        students = _load_students_from_grades(class_id)

    # Batch-fetch free passes for all students in one query (fixes N+1)
    if students:
        students.sort(key=_student_row_sort_key)
        student_ids = [s['id'] for s in students if s.get('id')]
        passes_map = _batch_get_free_passes(student_ids, class_id)
        for prof in students:
            prof['passes_remaining'] = max(0, hw_passes_allowed - passes_map.get(prof['id'], 0))

    lo_names = []
    try:
        lo_names = Course.get_learning_objectives(class_id)
    except Exception as e:
        logger.error("Error loading LOs: %s", e)

    return render_template("class_speed_grader.html",
                           class_id=class_id,
                           class_name=class_data.get('name'),
                           assignments=assignments,
                           students=students,
                           lo_names=lo_names,
                           hw_passes_allowed=hw_passes_allowed,
                           auto_convert_m=class_data.get('auto_convert_m', False),
                           min_masteries=class_data.get('min_masteries', 2))

@main_bp.route("/class/<class_id>/update_grade", methods=["GET", "POST"], endpoint='upload_grades')
@login_required
def update_grade_handler(class_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))

    class_data = Course.get_full_class_data(class_id)

    if not class_data:
        return redirect(url_for('main.instructor_dashboard'))
    
    if request.method == "POST":
        # Block upload if no assignments exist
        assignments_check = supabase_admin.table("assignments") \
            .select("id") \
            .eq("class_id", class_id) \
            .limit(1) \
            .execute()
        if not assignments_check.data:
            return "Cannot upload grades: no assignments exist for this class", 400

        file = request.files.get('file')
        assignment_id = request.form.get('assignment_id')

        if not file or file.filename == '':
            return "No file selected", 400

        logger.info("File uploaded for class %s: %s, assignment_id=%s", class_id, file.filename, assignment_id)

        # Placeholder for grade import parsing implementation.
        # Currently we just redirect back to the class detail page.
        return redirect(url_for('main.class_detail', class_id=class_id))

    assignments = load_assignments_for_class(class_id)
    template_kwargs = {
        "class_id": class_id,
        "class_name": class_data.get("name"),
        "assignments": assignments,
    }
    if assignments:
        try:
            template_kwargs["learning_objectives"] = Course.get_learning_objectives(class_id) or []
        except Exception as e:
            logger.error("upload_grades: failed to load LOs for class %s: %s", class_id, e)
            template_kwargs["learning_objectives"] = []
        mobile_upload_token = str(uuid4())
        _pending_put(mobile_upload_token, class_id, session["user_id"])
        mobile_upload_url = (
            f"{_public_base_url()}/class/{class_id}/mobile-upload/{mobile_upload_token}"
        )
        template_kwargs["mobile_upload_token"] = mobile_upload_token
        template_kwargs["mobile_upload_url"] = mobile_upload_url
        template_kwargs["mobile_upload_url_is_loopback"] = _url_looks_like_loopback(mobile_upload_url)

    return render_template("update_grade.html", **template_kwargs)

@main_bp.route("/class/<class_id>/mobile-upload/<token>")
def mobile_upload_page(class_id, token):
    """Phone-friendly page to photograph or pick a grade sheet (opened via QR)."""
    p = _pending_get(token)
    if not p or p["class_id"] != class_id:
        return (
            render_template("mobile_upload.html", error="This link is invalid or has expired.", upload_kind="grades"),
            404,
        )
    upload_kind = p.get("upload_kind", "grades")
    return render_template(
        "mobile_upload.html",
        class_id=class_id,
        token=token,
        error=None,
        upload_kind=upload_kind,
    )


@main_bp.route("/api/mobile-upload/<token>", methods=["POST"])
@rate_limited("mobile_upload_receive", limit=60, window_sec=300)
def mobile_upload_receive(token):
    """Receive file from phone; token proves intent (short-lived, unguessable)."""
    p = _pending_get(token)
    if not p:
        return jsonify({"success": False, "error": "Invalid or expired link"}), 400
    if p.get("uploaded"):
        return jsonify({"success": False, "error": "This upload link has already been used"}), 409

    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file uploaded"}), 400

    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"success": False, "error": "No file selected"}), 400

    upload_kind = p.get("upload_kind", "grades")
    fn_lower = f.filename.lower()
    if upload_kind == "lo_outcomes":
        allowed_extensions = (".csv",)
        if not fn_lower.endswith(allowed_extensions):
            return jsonify({"success": False, "error": "Use a CSV file"}), 400
        max_size = 5 * 1024 * 1024
    else:
        allowed_extensions = (".pdf", ".jpg", ".jpeg", ".png")
        if not fn_lower.endswith(allowed_extensions):
            return jsonify({"success": False, "error": "Use a PDF, JPG, or PNG"}), 400
        max_size = 10 * 1024 * 1024

    data = f.read()
    if len(data) > max_size:
        return jsonify({"success": False, "error": "File is too large"}), 400
    if upload_kind == "grades":
        if not _allowed_grade_upload_signature(data, f.filename):
            return jsonify({"success": False, "error": "File contents do not match allowed format"}), 400
    else:
        try:
            data.decode("utf-8")
        except Exception:
            return jsonify({"success": False, "error": "CSV must be UTF-8 text"}), 400

    content_type = f.mimetype or "application/octet-stream"
    with _mobile_upload_lock:
        if token not in _pending_mobile_uploads:
            return jsonify({"success": False, "error": "Link expired"}), 400
        if _pending_mobile_uploads[token].get("uploaded"):
            return jsonify({"success": False, "error": "This upload link has already been used"}), 409
        _pending_mobile_uploads[token]["file"] = data
        _pending_mobile_uploads[token]["filename"] = f.filename
        _pending_mobile_uploads[token]["content_type"] = content_type
        _pending_mobile_uploads[token]["uploaded"] = True

    return jsonify({"success": True})


@main_bp.route("/api/class/<class_id>/mobile-upload-status/<token>")
@api_login_required
def mobile_upload_status(class_id, token):
    """Desktop polls until the phone has uploaded a file."""
    p = _pending_get(token)
    if not p or p["class_id"] != class_id or p["user_id"] != session["user_id"]:
        return jsonify({"success": False, "error": "Not found"}), 404
    ready = p["file"] is not None
    return jsonify(
        {
            "success": True,
            "ready": ready,
            "filename": p["filename"] if ready else None,
        }
    )


@main_bp.route("/api/class/<class_id>/mobile-upload-file/<token>")
@api_login_required
def mobile_upload_file(class_id, token):
    """Return uploaded bytes once, then clear the handoff."""
    p = _pending_get(token)
    if not p or p["class_id"] != class_id or p["user_id"] != session["user_id"]:
        abort(404)
    if p["file"] is None:
        return jsonify({"success": False, "error": "No file yet"}), 404

    data = p["file"]
    filename = p["filename"] or "upload.jpg"
    content_type = p["content_type"] or "application/octet-stream"
    _pending_delete(token)

    safe_name = secure_filename(str(filename)) or "upload.bin"
    return send_file(
        io.BytesIO(data),
        mimetype=content_type,
        as_attachment=False,
        download_name=safe_name,
    )


@main_bp.route("/class/<class_id>/create_learning_objective", methods=["GET", "POST"], endpoint='create_learning_objective')
@login_required
def create_lo_handler(class_id):
    if not _instructor_owns_class(class_id):
        return redirect(url_for('main.instructor_dashboard'))

    class_data = Course.get_full_class_data(class_id)
    if not class_data:
        return redirect(url_for('main.instructor_dashboard'))

    if request.method == "POST":
        # Create a new learning objective. (Assignment creation/editing is
        # handled separately by create_assignment / update_assignment, which
        # the current UI actually calls.)
        lo_code = request.form.get('code', '').strip()
        lo_description = request.form.get('description', '').strip()
        lo_required_ms = request.form.get('required_ms', 2)

        if lo_code and len(lo_code) > 50:
            return "Objective code must be 50 characters or fewer.", 400
        if lo_description and len(lo_description) > 2000:
            return "Description must be 2000 characters or fewer.", 400
        if lo_code:
            try:
                req_ms = int(lo_required_ms)
            except (TypeError, ValueError):
                req_ms = DEFAULT_REQUIRED_MS
            req_ms = max(1, min(5, req_ms))
            try:
                _insert_learning_objectives_compat({
                    "class_id": class_id,
                    "vendor_code": lo_code,
                    "description": lo_description or None,
                    "required_ms": req_ms
                })
            except Exception as e:
                logger.error("Error creating LO: %s", e)

        if request.form.get("return_to", "").strip() == "dashboard":
            return redirect(url_for("main.class_detail", class_id=class_id))
        return redirect(url_for('main.class_assignments', class_id=class_id))

    # GET requests just redirect back to the objectives page (modal handles creation)
    return redirect(url_for('main.class_assignments', class_id=class_id))

@main_bp.route("/support")
def support():
    return render_template("support.html")

# ============================================================================
# API & ACTION ROUTES
# ============================================================================

@main_bp.route("/add_class", methods=["POST"])
@login_required
def add_class():
    # Defense-in-depth: the create_class_token below is only ever issued by
    # instructor_dashboard (which itself checks role == 'instructor'), so a
    # non-instructor session shouldn't be able to reach this point at all
    # today. Checking the role directly here too means this can't silently
    # regress if the token-issuing flow is ever refactored.
    if (session.get('role') or '').strip().lower() != 'instructor':
        return redirect(url_for('main.login_page'))

    if not request.form.get("name"):
        return "Class name is required.", 400
    if len(request.form.get("name", "").strip()) > 255:
        return "Class name must be 255 characters or fewer.", 400

    form_token = (request.form.get("create_class_token") or "").strip()
    session_token = (session.get("create_class_token") or "").strip()
    if not form_token or form_token != session_token:
        return redirect(url_for('main.instructor_dashboard'))
    if not _consume_one_time_form_token("create_class", form_token):
        return redirect(url_for('main.instructor_dashboard'))
    # Rotate token immediately so refresh/back cannot replay this post.
    session['create_class_token'] = str(uuid4())

    user_id = session['user_id']

    try:
        # Always upsert the profile first to satisfy the foreign key constraint.
        ensure_profile_exists(
            user_id,
            full_name=session.get('full_name'),
            role=session.get('role', 'instructor')
        )

        semester = request.form.get("semester", "")
        year = request.form.get("year", "")
        semester_full = f"{semester} {year}".strip() if year else semester
        if len(semester_full) > 100:
            return "Semester must be 100 characters or fewer.", 400

        new_class_data = {
            "name": request.form.get("name"),
            "semester": semester_full,
            "instructor_id": user_id,
        }
        # Optional columns — only include if the form provides them.
        # Each requires a matching column in the Supabase classes table.
        optional_fields = {
            "days": request.form.get("days", ""),
            "num_learning_objectives": int(request.form.get("num_learning_objectives") or 0),
            "min_masteries": int(request.form.get("min_masteries") or 2),
            "is_online": request.form.get("is_online") == "1",
        }
        if request.form.get("auto_convert_m") == "1":
            optional_fields["auto_convert_m"] = True

        # Try inserting with all fields first; if a column is missing, retry without optional fields
        try:
            full_data = {**new_class_data, **optional_fields}
            supabase_admin.table("classes").insert(full_data).execute()
        except Exception as col_err:
            if 'PGRST204' in str(col_err) or 'schema cache' in str(col_err):
                # If the new online flag column doesn't exist yet, retry without it first
                if "is_online" in full_data:
                    reduced_data = dict(full_data)
                    reduced_data.pop("is_online", None)
                    try:
                        supabase_admin.table("classes").insert(reduced_data).execute()
                    except Exception as reduced_err:
                        if 'PGRST204' in str(reduced_err) or 'schema cache' in str(reduced_err):
                            # Last fallback: insert only core columns
                            supabase_admin.table("classes").insert(new_class_data).execute()
                        else:
                            raise
                else:
                    # Fallback: insert only core columns
                    supabase_admin.table("classes").insert(new_class_data).execute()
            else:
                raise
        return redirect(url_for('main.instructor_dashboard'))
    except Exception as e:
        logger.error("Failed to create class: %s", e)
        return "Failed to create class.", 500


@main_bp.route("/api/update_grade", methods=["POST"], endpoint='api_update_grade')
@api_login_required
def api_update_grade():
    data = request.get_json() or {}
    try:
        supplied_class_id = data.get("class_id")
        assignment_id = data.get("assignment_id")
        lo_id = data.get("lo_id")

        # Always derive the authoritative class_id from the related entities
        # (assignment / learning objective) — never trust the caller's value
        # alone, since they could send a class_id they own while passing a
        # foreign assignment_id / lo_id.
        derived_from_assignment = None
        derived_from_lo = None

        if assignment_id:
            try:
                a = (
                    supabase_admin.table("assignments")
                    .select("class_id")
                    .eq("id", assignment_id)
                    .limit(1)
                    .execute()
                )
                if a.data:
                    derived_from_assignment = a.data[0].get("class_id")
            except Exception:
                derived_from_assignment = None
        if lo_id:
            try:
                lo = (
                    supabase_admin.table("learning_objectives")
                    .select("class_id")
                    .eq("id", lo_id)
                    .limit(1)
                    .execute()
                )
                if lo.data:
                    derived_from_lo = lo.data[0].get("class_id")
            except Exception:
                derived_from_lo = None

        # If both assignment and LO are present they must agree on a class.
        if (
            derived_from_assignment
            and derived_from_lo
            and str(derived_from_assignment) != str(derived_from_lo)
        ):
            return jsonify({"success": False, "error": "Forbidden"}), 403

        true_class_id = derived_from_assignment or derived_from_lo

        # If the caller supplied a class_id, it must match the derived one.
        if (
            supplied_class_id
            and true_class_id
            and str(supplied_class_id) != str(true_class_id)
        ):
            return jsonify({"success": False, "error": "Forbidden"}), 403

        # Fall back to the caller-supplied value only when nothing could be
        # derived (e.g. legacy callers without assignment/lo). Ownership is
        # still enforced below.
        class_id = true_class_id or supplied_class_id

        if not class_id or not _instructor_owns_class(str(class_id)):
            return jsonify({"success": False, "error": "Forbidden"}), 403

        if not _student_enrolled_in_class(str(class_id), str(data.get('student_id') or '')):
            return jsonify({"success": False, "error": "Student is not enrolled in this class"}), 403

        if lo_id:
            allowed_lo_ids = _allowed_lo_ids_for_grading(
                str(class_id), str(assignment_id) if assignment_id else None
            )
            if str(lo_id) not in allowed_lo_ids:
                return jsonify({
                    "success": False,
                    "error": "Invalid learning objective for this assignment",
                }), 400

        normalized = Grade.normalize_score(data.get("top_score"))
        if assignment_id and normalized and normalized != "A":
            hw_map = Homework.get_hw_scores_map_for_assignment(
                str(class_id), str(assignment_id)
            )
            if not Homework.student_has_recorded_score(hw_map, data.get("student_id")):
                return jsonify({
                    "success": False,
                    "error": _HW_REQUIRED_FOR_MARK_ERROR,
                }), 400

        Grade.update_score(student_id=data['student_id'], lo_id=data['lo_id'], 
                            top_score=data['top_score'], second_score=data.get('second_score'),
                            assignment_id=data.get('assignment_id'), changed_by=session['user_id'])
        _log_grade_upserts(
            [{
                "student_id": data["student_id"],
                "learning_objective_id": data["lo_id"],
                "assignment_id": data.get("assignment_id"),
                "top_score": Grade.normalize_score(data.get("top_score")),
                "counts_for_mastery": None,
            }],
            str(class_id),
            session["user_id"],
        )
        return jsonify({"success": True})
    except Exception as e:
        return _safe_api_error("Could not update grade", 500, log_detail=e)


@main_bp.route("/api/class/<class_id>/assignments")
@api_login_required
def api_class_assignments(class_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    try:
        result = supabase_admin.table("assignments") \
            .select("*") \
            .eq("class_id", class_id) \
            .order("created_at", desc=False) \
            .execute()
        return jsonify({"success": True, "assignments": result.data or []}), 200
    except Exception as e:
        return _safe_api_error("Could not load assignments", 500, log_detail=e)


@main_bp.route("/api/class/<class_id>/save-grades", methods=["POST"])
@api_login_required
def save_grades(class_id):
    try:
        if not _instructor_owns_class(class_id):
            return jsonify({"success": False, "error": "Forbidden"}), 403
        data = request.get_json()
        grades_dict = data.get('grades', {})
        assignment_id = data.get('assignment_id')
        if assignment_id and not _assignment_belongs_to_class(class_id, str(assignment_id)):
            return jsonify({"success": False, "error": "Invalid assignment for class"}), 400
        allowed_lo_ids = _allowed_lo_ids_for_grading(
            class_id, str(assignment_id) if assignment_id else None
        )
        enrolled_student_ids = _enrolled_student_ids_for_class(class_id)
        auto_convert = _class_auto_convert_m_enabled(class_id) if assignment_id else False
        hw_map = Homework.get_hw_scores_map_for_assignment(class_id, assignment_id) if assignment_id else {}

        # Pre-collect the (student, lo) pairs we're about to write so we can
        # batch-fetch the existing rows in ONE query and implement first-entry-
        # only stickiness for the counts_for_mastery flag. Cells with an empty/
        # cleared value are routed to `to_clear` instead of being silently
        # dropped -- an explicit clear should delete any existing grade row.
        incoming = []
        to_clear = []
        for key, grade_value in grades_dict.items():
            parts = key.split('|')
            if len(parts) != 2:
                continue
            student_id, lo_id = parts
            if not lo_id:
                continue
            if str(lo_id) not in allowed_lo_ids:
                continue
            if str(student_id).strip() not in enrolled_student_ids:
                continue
            if not grade_value:
                to_clear.append({'student_id': student_id, 'lo_id': lo_id})
                continue
            normalized = Grade.normalize_score(grade_value)
            if not normalized:
                continue
            if (
                assignment_id
                and normalized != "A"
                and not Homework.student_has_recorded_score(hw_map, student_id)
            ):
                return jsonify({
                    "success": False,
                    "error": _HW_REQUIRED_FOR_MARK_ERROR,
                }), 400
            incoming.append({
                'student_id': student_id,
                'lo_id': lo_id,
                'normalized': normalized,
                'sid_key': str(student_id).strip(),
            })

        # Batch-fetch existing rows so we can preserve the original
        # counts_for_mastery for any row that already holds an M/MR. A row is
        # only "first-entry" if no row exists yet or the existing row holds a
        # non-mastery letter (R, RQ, P, X, A). Wide select with a fallback for
        # deployments that have not yet run scripts/add_grades_counts_for_mastery.sql.
        existing_by_key: Dict[str, Dict[str, Any]] = {}
        if incoming and assignment_id:
            sids = list({it['student_id'] for it in incoming})
            lo_ids = list({it['lo_id'] for it in incoming})
            try:
                ex_resp = supabase_admin.table("grades").select(
                    "student_id, learning_objective_id, top_score, counts_for_mastery"
                ).in_("student_id", sids).in_("learning_objective_id", lo_ids).eq(
                    "assignment_id", assignment_id
                ).execute()
            except Exception as schema_err:
                logger.debug(
                    "Wide save_grades pre-fetch failed (likely missing counts_for_mastery); falling back: %s",
                    schema_err,
                )
                try:
                    ex_resp = supabase_admin.table("grades").select(
                        "student_id, learning_objective_id, top_score"
                    ).in_("student_id", sids).in_("learning_objective_id", lo_ids).eq(
                        "assignment_id", assignment_id
                    ).execute()
                except Exception as e:
                    logger.error("save_grades existing pre-fetch failed: %s", e)
                    ex_resp = None
            if ex_resp is not None:
                for r in (ex_resp.data or []):
                    k = f"{r['student_id']}|{r['learning_objective_id']}"
                    existing_by_key[k] = r

        # Build batch of grade rows and upsert in one call.
        grade_rows = []
        for it in incoming:
            student_id = it['student_id']
            lo_id = it['lo_id']
            normalized = it['normalized']
            sid = it['sid_key']
            key = f"{student_id}|{lo_id}"

            # counts_for_mastery semantics: only meaningful when the saved
            # letter is M or MR. Default True for everything else so the column
            # stays simple. Non-counting status is FIRST-ENTRY-ONLY: once a row
            # holds M/MR with a flag, re-saving M/MR keeps the existing flag,
            # even if HW% has changed.
            counts_for_mastery = True
            if normalized in ('M', 'MR'):
                existing = existing_by_key.get(key)
                existing_top = (existing or {}).get('top_score')
                if existing and existing_top in ('M', 'MR'):
                    prev_flag = existing.get('counts_for_mastery')
                    counts_for_mastery = True if prev_flag is None else bool(prev_flag)
                elif auto_convert:
                    # First-entry M/MR: capture HW eligibility. Non-A marks
                    # already required a recorded HW score above, so sid is in hw_map.
                    if sid in hw_map:
                        counts_for_mastery = bool(
                            Homework.is_exam_grade_eligible_hw_score(hw_map[sid])
                        )
                    else:
                        counts_for_mastery = True
                else:
                    counts_for_mastery = True

            row = {
                "student_id": student_id,
                "learning_objective_id": lo_id,
                "top_score": normalized,
                "counts_for_mastery": counts_for_mastery,
                "last_modified_by": session['user_id'],
            }
            if assignment_id:
                row["assignment_id"] = assignment_id
            grade_rows.append(row)

        if grade_rows:
            try:
                supabase_admin.table("grades").upsert(
                    grade_rows, on_conflict="student_id,learning_objective_id,assignment_id"
                ).execute()
            except Exception as schema_err:
                # Fallback for deployments that have not yet run the migration:
                # strip counts_for_mastery/last_modified_by and try again so saves don't 400.
                msg = str(schema_err)
                if "counts_for_mastery" in msg or "last_modified_by" in msg or "PGRST204" in msg or "schema cache" in msg.lower():
                    logger.debug(
                        "save_grades upsert failed on counts_for_mastery/last_modified_by (migration not run); retrying without them"
                    )
                    legacy_rows = [
                        {k: v for k, v in r.items() if k not in ('counts_for_mastery', 'last_modified_by')}
                        for r in grade_rows
                    ]
                    supabase_admin.table("grades").upsert(
                        legacy_rows, on_conflict="student_id,learning_objective_id,assignment_id"
                    ).execute()
                else:
                    raise

        _log_grade_upserts(grade_rows, class_id, session.get("user_id"))

        if to_clear:
            _clear_grade_cells_for_assignment(
                class_id, assignment_id, to_clear, session.get("user_id")
            )

        return jsonify({"success": True})
    except Exception as e:
        return _safe_api_error("Could not save grades", 500, log_detail=e)


@main_bp.route("/api/class/<class_id>/assignment/<assignment_id>/grades")
@api_login_required
def api_assignment_grades(class_id, assignment_id):
    """Return grades for a specific assignment, keyed by student_id|lo_id."""
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    if not _assignment_belongs_to_class(class_id, assignment_id):
        return jsonify({"success": False, "error": "Invalid assignment for class"}), 400
    try:
        # Widest projection first (includes counts_for_mastery); deployments
        # that haven't run scripts/add_grades_counts_for_mastery.sql fall back
        # to the narrower select. Missing flag => default True.
        # supabase-py requires .select() before filters like .eq(); callers pass
        # a filter lambda that receives the already-`select()`ed builder.
        def _select_grade_rows(apply_filters):
            try:
                return apply_filters(
                    supabase_admin.table("grades").select(
                        "student_id, learning_objective_id, top_score, counts_for_mastery"
                    )
                ).execute()
            except Exception as schema_err:
                logger.debug(
                    "Wide assignment grades select failed (likely missing counts_for_mastery); falling back: %s",
                    schema_err,
                )
                return apply_filters(
                    supabase_admin.table("grades").select(
                        "student_id, learning_objective_id, top_score"
                    )
                ).execute()

        result = _select_grade_rows(
            lambda q: q.eq("assignment_id", assignment_id)
        )
        grades_map = {}
        counts_for_mastery_map = {}
        has_assignment_grades = False
        for g in (result.data or []):
            if str(g.get("top_score") or "").strip():
                has_assignment_grades = True
            key = f"{g['student_id']}|{g['learning_objective_id']}"
            grades_map[key] = g['top_score']
            flag = g.get('counts_for_mastery')
            counts_for_mastery_map[key] = True if flag is None else bool(flag)

        # Import / legacy rows often have assignment_id NULL while still targeting LOs on this
        # assignment. Class reports load all grade rows per student, so those marks appeared
        # there but not here when we only filtered by assignment_id. Fill gaps from unscoped rows
        # for LOs linked to this assignment (explicit assignment-scoped rows win if both exist).
        # When assignment_objectives has no rows yet (e.g. PDF-imported assignments that were
        # never edited in the UI), fall back to every LO in the class so legacy grades still
        # surface here the same way they do on the Reports tab.
        ao_resp = supabase_admin.table("assignment_objectives") \
            .select("learning_objective_id") \
            .eq("assignment_id", assignment_id) \
            .execute()
        lo_ids = [
            r["learning_objective_id"]
            for r in (ao_resp.data or [])
            if r.get("learning_objective_id")
        ]
        if not lo_ids:
            try:
                lo_ids = Course.get_lo_ids_for_class(class_id) or []
            except Exception as lo_err:
                logger.error(
                    "Error loading class LO ids for unscoped grade fallback (class=%s): %s",
                    class_id,
                    lo_err,
                )
                lo_ids = []
        if lo_ids:
            unscoped = _select_grade_rows(
                lambda q: q.is_("assignment_id", None).in_("learning_objective_id", lo_ids)
            )
            for g in (unscoped.data or []):
                key = f"{g['student_id']}|{g['learning_objective_id']}"
                if key not in grades_map:
                    grades_map[key] = g["top_score"]
                    flag = g.get('counts_for_mastery')
                    counts_for_mastery_map[key] = True if flag is None else bool(flag)

        # HW % is shared by all assignments in the same homework_group (see Homework.get_hw_scores_map_for_assignment)
        hw_map = Homework.get_hw_scores_map_for_assignment(class_id, assignment_id)

        # Compute revision eligibility per student (HW >= 65% or pass used)
        eligibility = {}
        for sid, score in hw_map.items():
            eligibility[sid] = score == -1 or (
                score is not None and score >= Homework.EXAM_GRADE_HW_THRESHOLD
            )

        assignment_type = "mastery_opp"
        try:
            asg_resp = (
                supabase_admin.table("assignments")
                .select("id, assignment_type")
                .eq("id", assignment_id)
                .eq("class_id", class_id)
                .limit(1)
                .execute()
            )
            asg_row = (asg_resp.data or [None])[0] or {}
            raw_type = asg_row.get("assignment_type") or "mastery_opp"
            assignment_type = raw_type if raw_type in ASSIGNMENT_TYPES else "mastery_opp"
        except Exception as type_err:
            logger.debug("api_assignment_grades: could not load assignment_type: %s", type_err)

        return jsonify({
            "success": True,
            "grades": grades_map,
            "has_assignment_grades": has_assignment_grades,
            "counts_for_mastery_map": counts_for_mastery_map,
            "hw_scores": hw_map,
            "revision_eligible": eligibility,
            "assignment_type": assignment_type,
        })
    except Exception as e:
        return _safe_api_error("Could not load assignment grades", 500, log_detail=e)


def _promote_non_counting_masteries_for_student(class_id: str, student_id: str, changed_by: str = None) -> int:
    """When a student uses an HW pass, restore all their non-counting M/MR rows."""
    lo_ids = Course.get_all_lo_ids_for_class(class_id)
    if not lo_ids:
        return 0
    try:
        resp = supabase_admin.table("grades").select(
            "student_id, learning_objective_id, assignment_id, top_score, counts_for_mastery"
        ).eq("student_id", student_id).in_("learning_objective_id", lo_ids).in_(
            "top_score", ["M", "MR"]
        ).execute()
    except Exception as e:
        logger.warning(
            "promote non-counting masteries select failed (wide): %s", e
        )
        try:
            resp = supabase_admin.table("grades").select(
                "student_id, learning_objective_id, assignment_id, top_score"
            ).eq("student_id", student_id).in_("learning_objective_id", lo_ids).in_(
                "top_score", ["M", "MR"]
            ).execute()
        except Exception:
            return 0

    rows = []
    for g in resp.data or []:
        if g.get("counts_for_mastery") is not False:
            continue
        row = {
            "student_id": student_id,
            "learning_objective_id": g["learning_objective_id"],
            "top_score": g["top_score"],
            "counts_for_mastery": True,
        }
        if changed_by is not None:
            row["last_modified_by"] = changed_by
        aid = g.get("assignment_id")
        if aid is not None:
            row["assignment_id"] = aid
        rows.append(row)

    if not rows:
        return 0

    try:
        supabase_admin.table("grades").upsert(
            rows, on_conflict="student_id,learning_objective_id,assignment_id"
        ).execute()
    except Exception as e:
        msg = str(e)
        if "counts_for_mastery" in msg or "PGRST204" in msg or "schema cache" in msg.lower():
            logger.warning(
                "promote non-counting masteries upsert skipped (migration not run): %s",
                e,
            )
            return 0
        raise
    _log_grade_upserts(rows, class_id, changed_by)
    return len(rows)


@main_bp.route("/api/class/<class_id>/save-hw-percentage", methods=["POST"])
@api_login_required
def save_hw_percentage(class_id):
    try:
        if not _instructor_owns_class(class_id):
            return jsonify({"success": False, "error": "Forbidden"}), 403
        data = request.get_json()
        student_id = data.get('student_id')
        score = data.get('score')
        assignment_id = data.get('assignment_id')
        if student_id is None or score is None or not assignment_id:
            return jsonify({"success": False, "error": "student_id, score, and assignment_id required"}), 400
        score = int(score)
        if score != -1:
            score = max(0, min(100, score))
        # Accept IDs with incidental whitespace from client-side state.
        class_id = str(class_id).strip()
        assignment_id = str(assignment_id).strip()
        if not _assignment_belongs_to_class(class_id, assignment_id):
            return jsonify({"success": False, "error": "Assignment not found for this class"}), 404
        if not _student_enrolled_in_class(class_id, str(student_id).strip()):
            return jsonify({"success": False, "error": "Student is not enrolled in this class"}), 403

        hw_key = Homework.resolve_hw_group_storage_key(class_id, assignment_id)
        if hw_key is None:
            client_hg = Homework.normalize_homework_group(data.get("homework_group"))
            hw_key = client_hg or assignment_id
        hw_group = str(hw_key).strip()
        row = {
            "student_id": student_id,
            "class_id": class_id,
            "homework_group": hw_group,
            "score_pct": score,
        }
        # Single-row list so PostgREST gets explicit columns=… (more reliable than a bare dict).
        supabase_admin.table("homework_scores").upsert(
            [row],
            on_conflict="student_id,class_id,homework_group",
        ).execute()

        promoted = 0
        if score == -1:
            promoted = _promote_non_counting_masteries_for_student(class_id, student_id, changed_by=session['user_id'])

        return jsonify({"success": True, "promoted_masteries": promoted})
    except Exception as e:
        logger.error("saving hw percentage failed: %s", e)
        return _safe_api_error("Could not save homework score", 500, log_detail=e)


@main_bp.route("/api/class/<class_id>/use_pass", methods=["POST"])
@api_login_required
def use_free_pass(class_id):
    try:
        if not _instructor_owns_class(class_id):
            return jsonify({"success": False, "error": "Forbidden"}), 403
        data = request.get_json()
        student_id = data.get('student_id')
        if not student_id:
            return jsonify({"success": False, "error": "student_id required"}), 400
        if not _student_enrolled_in_class(class_id, str(student_id)):
            return jsonify({"success": False, "error": "Student is not enrolled in this class"}), 403

        passes_allowed = 2

        existing = supabase_admin.table("free_passes") \
            .select("id, passes_used") \
            .eq("student_id", student_id) \
            .eq("class_id", class_id) \
            .execute()

        if existing.data:
            current_used = existing.data[0]['passes_used']
            if current_used >= passes_allowed:
                return jsonify({"success": False, "error": "No passes remaining"}), 400
            supabase_admin.table("free_passes") \
                .update({"passes_used": current_used + 1}) \
                .eq("id", existing.data[0]['id']) \
                .execute()
            remaining = passes_allowed - (current_used + 1)
        else:
            supabase_admin.table("free_passes").insert({
                "student_id": student_id,
                "class_id": class_id,
                "passes_used": 1
            }).execute()
            remaining = passes_allowed - 1

        return jsonify({"success": True, "passes_remaining": remaining})
    except Exception as e:
        logger.error("use pass failed: %s", e)
        return _safe_api_error("Could not use free pass", 500, log_detail=e)


@main_bp.route("/api/class/<class_id>/return_pass", methods=["POST"])
@api_login_required
def return_free_pass(class_id):
    try:
        if not _instructor_owns_class(class_id):
            return jsonify({"success": False, "error": "Forbidden"}), 403
        data = request.get_json()
        student_id = data.get('student_id')
        if not student_id:
            return jsonify({"success": False, "error": "student_id required"}), 400
        if not _student_enrolled_in_class(class_id, str(student_id)):
            return jsonify({"success": False, "error": "Student is not enrolled in this class"}), 403

        passes_allowed = 2

        existing = supabase_admin.table("free_passes") \
            .select("id, passes_used") \
            .eq("student_id", student_id) \
            .eq("class_id", class_id) \
            .execute()

        if existing.data and existing.data[0]['passes_used'] > 0:
            current_used = existing.data[0]['passes_used']
            supabase_admin.table("free_passes") \
                .update({"passes_used": current_used - 1}) \
                .eq("id", existing.data[0]['id']) \
                .execute()
            remaining = passes_allowed - (current_used - 1)
        else:
            remaining = passes_allowed

        return jsonify({"success": True, "passes_remaining": remaining})
    except Exception as e:
        logger.error("return pass failed: %s", e)
        return _safe_api_error("Could not return free pass", 500, log_detail=e)


@main_bp.route("/api/class/<class_id>/available_students", methods=["GET"])
@api_instructor_required
def get_available_students(class_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    try:
        enrolled_ids = {e['student_id'] for e in
                        supabase_admin.table("enrollments").select("student_id")
                        .eq("class_id", class_id).execute().data or []}

        # Only offer students who already have a relationship with this
        # instructor (enrolled in one of their other classes) — never a
        # dump of every student profile in the deployment.
        instructor_classes = Course.get_all_for_instructor(session["user_id"]) or []
        instructor_class_ids = [c["id"] for c in instructor_classes if c.get("id")]

        candidate_ids: set = set()
        if instructor_class_ids:
            enr_resp = (
                supabase_admin.table("enrollments")
                .select("student_id")
                .in_("class_id", instructor_class_ids)
                .execute()
            )
            candidate_ids = {r["student_id"] for r in (enr_resp.data or []) if r.get("student_id")}
        candidate_ids -= enrolled_ids

        all_students = []
        if candidate_ids:
            prof_resp = (
                supabase_admin.table("profiles")
                .select("id, full_name")
                .eq("role", "student")
                .in_("id", list(candidate_ids))
                .execute()
            )
            all_students = prof_resp.data or []

        available = sorted(all_students, key=_student_row_sort_key)
        for s in available:
            s["name"] = _student_display_name(
                {"id": s.get("id"), "full_name": s.get("full_name")}
            )
        return jsonify({"success": True, "students": available}), 200
    except Exception as e:
        return _safe_api_error("Could not load available students", 500, log_detail=e)


@main_bp.route("/api/class/<class_id>/add_student", methods=["POST"])
@api_instructor_required
def api_add_student_to_class(class_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    try:
        data = request.get_json()
        student_name = data.get('student_name', '').strip()
        if not student_name:
            return jsonify({"success": False, "error": "student_name is required"}), 400
        if len(student_name) > 255:
            return jsonify({"success": False, "error": "Student name must be 255 characters or fewer."}), 400
        student_id = str(uuid4())
        student_email = (data.get("student_email") or data.get("email") or "").strip()
        insert_row = {
            "id": student_id,
            "full_name": student_name,
            "sort_name": _generate_sort_name(student_name),
            "role": "student",
        }
        if student_email:
            insert_row["email"] = student_email
        try:
            supabase_admin.table("profiles").insert(insert_row).execute()
        except Exception as ins_err:
            insert_row.pop("email", None)
            logger.warning(
                "api add_student profiles insert with email failed (%s); retrying without email.",
                ins_err,
            )
            supabase_admin.table("profiles").insert(insert_row).execute()
        supabase_admin.table("enrollments").insert({
            "class_id": class_id, "student_id": student_id
        }).execute()
        return jsonify({"success": True, "student_id": student_id, "student_name": student_name}), 200
    except Exception as e:
        return _safe_api_error("Could not add student", 500, log_detail=e)


def _build_student_upload_enrollment_indexes(class_id: str):
    """Return class enrollment indexes used by CSV preview/import student upload flows."""
    enroll_resp = (
        supabase_admin.table("enrollments")
        .select("student_id")
        .eq("class_id", class_id)
        .execute()
    )
    enrolled_ids = [str(r.get("student_id")) for r in (enroll_resp.data or []) if r.get("student_id")]
    enrolled_set = set(enrolled_ids)

    enrolled_profiles: List[Dict[str, Any]] = []
    if enrolled_ids:
        prof_resp = (
            supabase_admin.table("profiles")
            .select("id, full_name, email")
            .in_("id", enrolled_ids)
            .execute()
        )
        enrolled_profiles = prof_resp.data or []

    enrolled_by_email: Dict[str, Dict[str, Any]] = {}
    enrolled_by_name: Dict[str, List[Dict[str, Any]]] = {}
    missing_email_by_name: Dict[str, List[Dict[str, Any]]] = {}
    for p in enrolled_profiles:
        sid = str(p.get("id") or "").strip()
        if not sid:
            continue
        profile = {
            "id": sid,
            "full_name": _normalize_spaces((p.get("full_name") or "").strip()),
            "email": _student_email_key((p.get("email") or "").strip()),
        }
        if profile["email"]:
            enrolled_by_email[profile["email"]] = profile
        nk = _student_name_key(profile["full_name"])
        enrolled_by_name.setdefault(nk, []).append(profile)
        if not profile["email"]:
            missing_email_by_name.setdefault(nk, []).append(profile)

    return enrolled_set, enrolled_by_email, enrolled_by_name, missing_email_by_name


def _profile_ids_with_enrollment_elsewhere(profile_ids: List[str], class_id: str) -> set:
    """Subset of profile_ids already enrolled in a class owned by a DIFFERENT
    instructor than the one who owns class_id.

    Used to avoid silently attaching a globally-matched profile (by email or
    name) that already belongs to a different instructor's roster onto this
    instructor's class. Deliberately NOT triggered by enrollment in another
    class owned by the SAME instructor — that's normal multi-class reuse and
    must keep working. Fails closed: a lookup error is treated as "claimed
    elsewhere" so we never attach when the safety check itself is broken.
    """
    ids = [str(pid) for pid in set(profile_ids) if pid]
    if not ids:
        return set()
    try:
        owner_id = _class_instructor_id(class_id)
        resp = (
            supabase_admin.table("enrollments")
            .select("student_id, class_id")
            .in_("student_id", ids)
            .execute()
        )
        rows = [r for r in (resp.data or []) if r.get("student_id")]
        other_class_ids = {
            str(r["class_id"]) for r in rows
            if r.get("class_id") and str(r["class_id"]) != str(class_id)
        }
        if not other_class_ids:
            return set()

        classes_resp = (
            supabase_admin.table("classes")
            .select("id, instructor_id")
            .in_("id", list(other_class_ids))
            .execute()
        )
        foreign_class_ids = {
            str(c["id"]) for c in (classes_resp.data or [])
            if c.get("id") and not _user_ids_equal(c.get("instructor_id"), owner_id)
        }
        if not foreign_class_ids:
            return set()
        return {
            str(r["student_id"]) for r in rows
            if str(r.get("class_id")) in foreign_class_ids
        }
    except Exception as e:
        logger.error("enrollment-elsewhere lookup failed: %s", e)
        return set(ids)


def _build_profiles_by_email_for_rows(rows: List[Dict[str, str]], class_id: str) -> Dict[str, Dict[str, Any]]:
    """Return global profile lookup by normalized email for provided CSV rows.

    Excludes matches already enrolled in a different class — a global email
    match belonging to another instructor's roster is treated as "no match"
    so we never silently attach a stranger's account to this class.
    """
    provided_email_keys = {
        _student_email_key(r.get("email") or "")
        for r in rows
        if _student_email_key(r.get("email") or "")
    }
    profiles_by_email: Dict[str, Dict[str, Any]] = {}
    if provided_email_keys:
        global_email_resp = (
            supabase_admin.table("profiles")
            .select("id, full_name, email")
            .in_("email", list(provided_email_keys))
            .execute()
        )
        candidates = global_email_resp.data or []
        claimed_elsewhere = _profile_ids_with_enrollment_elsewhere(
            [str(p.get("id")) for p in candidates if p.get("id")], class_id
        )
        for p in candidates:
            pid = str(p.get("id") or "")
            if pid and pid in claimed_elsewhere:
                continue
            ek = _student_email_key(p.get("email") or "")
            if ek:
                profiles_by_email[ek] = p
    return profiles_by_email


@main_bp.route("/api/class/<class_id>/upload_students", methods=["POST"])
@api_instructor_required
@rate_limited("upload_students", limit=20, window_sec=900)
def api_upload_students_to_class(class_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    err, rows, parse_warnings = _parse_student_csv_upload()
    if err:
        return jsonify({
            "success": False,
            "error": err,
            "warnings": parse_warnings,
        }), 400

    try:
        enrolled_set, enrolled_by_email, enrolled_by_name, missing_email_by_name = (
            _build_student_upload_enrollment_indexes(class_id)
        )
        profiles_by_email = _build_profiles_by_email_for_rows(rows, class_id)

        stats = {
            "created_profiles": 0,
            "enrolled_existing_profiles": 0,
            "updated_missing_emails": 0,
            "skipped_existing": 0,
        }
        upload_name_seen_count: Dict[str, int] = {}
        file_seen_emails: set = set()
        warnings = list(parse_warnings)

        for row in rows:
            full_name = _normalize_spaces(row.get("full_name") or "")
            if not full_name:
                continue
            name_key = _student_name_key(full_name)

            email = _student_email_key(row.get("email") or "")
            if email:
                if email in file_seen_emails:
                    warnings.append(f"Duplicate email in file skipped: {email}")
                    continue
                file_seen_emails.add(email)

                existing_in_class = enrolled_by_email.get(email)
                if existing_in_class:
                    stats["skipped_existing"] += 1
                    continue

                missing_candidates = missing_email_by_name.get(name_key) or []
                if missing_candidates:
                    target = missing_candidates.pop(0)
                    target_id = target.get("id")
                    if target_id:
                        supabase_admin.table("profiles").update({"email": email}).eq("id", target_id).execute()
                        target["email"] = email
                        enrolled_by_email[email] = target
                        profiles_by_email[email] = {
                            "id": target_id,
                            "full_name": target.get("full_name") or full_name,
                            "email": email,
                        }
                        stats["updated_missing_emails"] += 1
                        continue

                existing_profile = profiles_by_email.get(email)
                if existing_profile and existing_profile.get("id"):
                    existing_id = str(existing_profile.get("id"))
                    if existing_id not in enrolled_set:
                        supabase_admin.table("enrollments").insert({
                            "class_id": class_id,
                            "student_id": existing_id,
                        }).execute()
                        enrolled_set.add(existing_id)
                        stats["enrolled_existing_profiles"] += 1
                    else:
                        stats["skipped_existing"] += 1
                    enrolled_by_email[email] = {
                        "id": existing_id,
                        "full_name": _normalize_spaces(
                            (existing_profile.get("full_name") or full_name).strip()
                        ),
                        "email": email,
                    }
                    continue

                student_id = str(uuid4())
                insert_profile = {
                    "id": student_id,
                    "full_name": full_name,
                    "sort_name": _generate_sort_name(full_name),
                    "role": "student",
                    "email": email,
                }
                try:
                    supabase_admin.table("profiles").insert(insert_profile).execute()
                except Exception:
                    lookup = (
                        supabase_admin.table("profiles")
                        .select("id, full_name, email")
                        .eq("email", email)
                        .limit(1)
                        .execute()
                    )
                    if lookup.data:
                        found = lookup.data[0]
                        existing_id = str(found.get("id"))
                        if existing_id in _profile_ids_with_enrollment_elsewhere([existing_id], class_id):
                            # Email belongs to another instructor's student — don't
                            # attach; create a separate profile without that email.
                            new_id = str(uuid4())
                            supabase_admin.table("profiles").insert({
                                "id": new_id,
                                "full_name": full_name,
                                "sort_name": _generate_sort_name(full_name),
                                "role": "student",
                            }).execute()
                            supabase_admin.table("enrollments").insert({
                                "class_id": class_id,
                                "student_id": new_id,
                            }).execute()
                            enrolled_set.add(new_id)
                            enrolled_by_name.setdefault(name_key, []).append(
                                {"id": new_id, "full_name": full_name, "email": ""}
                            )
                            stats["created_profiles"] += 1
                            warnings.append(
                                f"Email {email} is already used by another account — "
                                f"created a separate profile for {full_name} without that email."
                            )
                            continue
                        if existing_id not in enrolled_set:
                            supabase_admin.table("enrollments").insert({
                                "class_id": class_id,
                                "student_id": existing_id,
                            }).execute()
                            enrolled_set.add(existing_id)
                            stats["enrolled_existing_profiles"] += 1
                        else:
                            stats["skipped_existing"] += 1
                        enrolled_by_email[email] = {
                            "id": existing_id,
                            "full_name": _normalize_spaces((found.get("full_name") or full_name).strip()),
                            "email": email,
                        }
                        continue
                    raise

                supabase_admin.table("enrollments").insert({
                    "class_id": class_id,
                    "student_id": student_id,
                }).execute()
                enrolled_set.add(student_id)
                enrolled_profile = {"id": student_id, "full_name": full_name, "email": email}
                enrolled_by_email[email] = enrolled_profile
                enrolled_by_name.setdefault(name_key, []).append(enrolled_profile)
                profiles_by_email[email] = enrolled_profile
                stats["created_profiles"] += 1
                continue

            seen_count = upload_name_seen_count.get(name_key, 0)
            upload_name_seen_count[name_key] = seen_count + 1
            existing_with_name = enrolled_by_name.get(name_key) or []
            if seen_count < len(existing_with_name):
                stats["skipped_existing"] += 1
                continue

            student_id = str(uuid4())
            supabase_admin.table("profiles").insert({
                "id": student_id,
                "full_name": full_name,
                "sort_name": _generate_sort_name(full_name),
                "role": "student",
            }).execute()
            supabase_admin.table("enrollments").insert({
                "class_id": class_id,
                "student_id": student_id,
            }).execute()
            enrolled_set.add(student_id)
            created = {"id": student_id, "full_name": full_name, "email": ""}
            enrolled_by_name.setdefault(name_key, []).append(created)
            missing_email_by_name.setdefault(name_key, []).append(created)
            stats["created_profiles"] += 1

        return jsonify({
            "success": True,
            "message": "Student upload complete",
            "stats": stats,
            "warnings": warnings,
            "total_rows": len(rows),
        })
    except Exception as e:
        logger.error("Error uploading students for class %s: %s", class_id, e)
        return _safe_api_error("Could not upload students", 500, log_detail=e)


@main_bp.route("/api/class/<class_id>/preview-upload-students", methods=["POST"])
@api_instructor_required
@rate_limited("preview_upload_students", limit=30, window_sec=900)
def api_preview_upload_students(class_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    err, rows, parse_warnings = _parse_student_csv_upload()
    if err:
        return jsonify({
            "success": False,
            "error": err,
            "warnings": parse_warnings,
        }), 400

    try:
        _, enrolled_by_email, enrolled_by_name, missing_email_by_name = (
            _build_student_upload_enrollment_indexes(class_id)
        )
        profiles_by_email = _build_profiles_by_email_for_rows(rows, class_id)

        preview_rows: List[Dict[str, Any]] = []
        stats = {
            "will_create": 0,
            "will_enroll_existing": 0,
            "will_update_missing_email": 0,
            "will_skip": 0,
        }
        upload_name_seen_count: Dict[str, int] = {}
        file_seen_emails: set = set()
        warnings = list(parse_warnings)

        for row in rows:
            full_name = _normalize_spaces(row.get("full_name") or "")
            if not full_name:
                continue
            name_key = _student_name_key(full_name)
            email = _student_email_key(row.get("email") or "")

            action = "skip"
            status = "Already in class"

            if email:
                if email in file_seen_emails:
                    action = "skip"
                    status = "Duplicate email in upload"
                    warnings.append(f"Duplicate email in file skipped: {email}")
                    stats["will_skip"] += 1
                else:
                    file_seen_emails.add(email)
                    if enrolled_by_email.get(email):
                        action = "skip"
                        status = "Already in class (email match)"
                        stats["will_skip"] += 1
                    else:
                        missing_candidates = missing_email_by_name.get(name_key) or []
                        if missing_candidates:
                            target = missing_candidates.pop(0)
                            target["email"] = email
                            enrolled_by_email[email] = target
                            action = "update_missing_email"
                            status = "Will attach email to existing student"
                            stats["will_update_missing_email"] += 1
                        else:
                            existing_profile = profiles_by_email.get(email)
                            if existing_profile and existing_profile.get("id"):
                                action = "enroll_existing"
                                status = "Will enroll existing profile by email"
                                stats["will_enroll_existing"] += 1
                                enrolled_by_email[email] = {
                                    "id": str(existing_profile.get("id")),
                                    "full_name": _normalize_spaces(
                                        (existing_profile.get("full_name") or full_name).strip()
                                    ),
                                    "email": email,
                                }
                            else:
                                action = "create"
                                status = "Will create and enroll"
                                stats["will_create"] += 1
                                placeholder = {"id": f"new-{len(preview_rows)}", "full_name": full_name, "email": email}
                                enrolled_by_email[email] = placeholder
                                enrolled_by_name.setdefault(name_key, []).append(placeholder)
                                profiles_by_email[email] = placeholder
            else:
                seen_count = upload_name_seen_count.get(name_key, 0)
                upload_name_seen_count[name_key] = seen_count + 1
                existing_with_name = enrolled_by_name.get(name_key) or []
                if seen_count < len(existing_with_name):
                    action = "skip"
                    status = "Already in class (name match)"
                    stats["will_skip"] += 1
                else:
                    action = "create"
                    status = "Will create and enroll (no email)"
                    stats["will_create"] += 1
                    placeholder = {"id": f"new-{len(preview_rows)}", "full_name": full_name, "email": ""}
                    enrolled_by_name.setdefault(name_key, []).append(placeholder)
                    missing_email_by_name.setdefault(name_key, []).append(placeholder)

            preview_rows.append(
                {
                    "full_name": full_name,
                    "email": email,
                    "action": action,
                    "status": status,
                }
            )

        return jsonify({
            "success": True,
            "rows": preview_rows,
            "stats": stats,
            "warnings": warnings,
            "count": len(preview_rows),
        })
    except Exception as e:
        logger.error("Error previewing student upload for class %s: %s", class_id, e)
        return _safe_api_error("Could not preview student upload", 500, log_detail=e)


@main_bp.route("/api/class/<class_id>/toggle_mute", methods=["POST"])
@api_instructor_required
def api_toggle_mute(class_id):
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    try:
        data = request.get_json()
        student_id = data.get('student_id')
        muted = bool(data.get('muted', False))
        if not student_id:
            return jsonify({"success": False, "error": "student_id is required"}), 400
        supabase_admin.table("enrollments").update({"muted": muted}) \
            .eq("class_id", class_id).eq("student_id", student_id).execute()
        return jsonify({"success": True, "muted": muted}), 200
    except Exception as e:
        return _safe_api_error("Could not update mute state", 500, log_detail=e)


@main_bp.route("/api/class/<class_id>/remove_student", methods=["POST"])
@api_instructor_required
def api_remove_student_from_class(class_id):
    try:
        if not _instructor_owns_class(class_id):
            return jsonify({"success": False, "error": "Forbidden"}), 403
        data = request.get_json()
        student_id = data.get('student_id')
        if not student_id:
            return jsonify({"success": False, "error": "student_id is required"}), 400
        supabase_admin.table("enrollments").delete() \
            .eq("class_id", class_id).eq("student_id", student_id).execute()

        # Also delete the student's grades for LOs belonging to this class
        lo_ids = Course.get_all_lo_ids_for_class(class_id)
        if lo_ids:
            supabase_admin.table("grades").delete() \
                .eq("student_id", student_id) \
                .in_("learning_objective_id", lo_ids).execute()

        return jsonify({"success": True}), 200
    except Exception as e:
        return _safe_api_error("Could not remove student", 500, log_detail=e)


@main_bp.route("/api/import-grades", methods=["POST"])
@api_login_required
@rate_limited("import_grades", limit=30, window_sec=900)
def api_import_grades():

    data = request.get_json() or {}
    class_id = data.get('class_id')
    assignment_id = data.get('assignment_id')
    students = data.get('students', []) or []
    extracted_los = data.get('learning_objectives', []) or []
    if not class_id:
        return jsonify({"success": False, "error": "Missing class_id"}), 400
    if not _instructor_owns_class(str(class_id)):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    if assignment_id and not _assignment_belongs_to_class(str(class_id), str(assignment_id)):
        return jsonify({"success": False, "error": "Invalid assignment for class"}), 400
    if len(students) > MAX_IMPORT_ROWS:
        return jsonify({"success": False, "error": f"Too many students in one import (max {MAX_IMPORT_ROWS})"}), 400

    # Keep import-only HW % columns out of mastery LO creation/linking.
    extracted_los = [
        lo for lo in extracted_los
        if not Homework.is_import_sheet_hw_column(lo)
    ]

    try:
        los_resp = _select_class_pool_learning_objectives(
            "id,vendor_code,description", class_id
        )
        existing_los = los_resp.data or []
    except Exception as e:
        return _safe_api_error("Failed to load learning objectives", 500, log_detail=e)

    lo_map = {}
    for lo in existing_los:
        if lo.get('vendor_code'):
            lo_map[lo['vendor_code'].strip().lower()] = lo['id']
        if lo.get('description'):
            lo_map[lo['description'].strip().lower()] = lo['id']

    # Create missing LOs from extracted list
    for lo_name in extracted_los:
        if not lo_name:
            continue
        key = lo_name.strip().lower()
        if key in lo_map:
            continue
        try:
            res = _insert_learning_objectives_compat({
                "class_id": class_id,
                "vendor_code": lo_name,
                "description": None
            })
            if res.data:
                lo_map[key] = res.data[0]['id']
        except Exception as e:
            logger.error("Error creating LO '%s': %s", lo_name, e)

    imported = 0
    grade_rows = []
    hw_rows = []
    to_clear_pairs: List[Dict[str, str]] = []
    hw_clear_sids: List[str] = []
    skipped_hw_warnings: List[str] = []
    hw_storage_key = (
        Homework.resolve_hw_group_storage_key(class_id, assignment_id)
        if assignment_id
        else None
    )
    hw_map = (
        dict(Homework.get_hw_scores_map_for_assignment(str(class_id), str(assignment_id)) or {})
        if assignment_id
        else {}
    )

    # Link all extracted LOs to the selected assignment (if not already linked)
    if assignment_id:
        try:
            existing_links_resp = supabase_admin.table("assignment_objectives") \
                .select("learning_objective_id") \
                .eq("assignment_id", assignment_id) \
                .execute()
            already_linked = set(r["learning_objective_id"] for r in (existing_links_resp.data or []))

            new_links = []
            for lo_name in extracted_los:
                if not lo_name:
                    continue
                lo_id = lo_map.get(lo_name.strip().lower())
                if lo_id and lo_id not in already_linked:
                    new_links.append({"assignment_id": assignment_id, "learning_objective_id": lo_id})
                    already_linked.add(lo_id)
            if new_links:
                supabase_admin.table("assignment_objectives").insert(new_links).execute()
                logger.info("Batch-linked %d LOs to assignment %s", len(new_links), assignment_id)
        except Exception as e:
            logger.error("Error linking LOs to assignment: %s", e)

    # We keep duplicate name rows in `student_entries` so grades for the same
    # student spread across two sheet rows still merge into one profile, but
    # `unique_names` drives the DB lookup so we never hit the same name twice
    # against PostgREST.
    student_entries: List[Tuple[str, Dict[str, Any]]] = []
    seen_keys: set = set()
    for s in students:
        nm = (s.get('name') or s.get('full_name') or '').strip()
        if not nm:
            continue
        key = nm.lower()
        if key in seen_keys:
            student_entries.append((nm, s))
            continue
        seen_keys.add(key)
        student_entries.append((nm, s))

    unique_names = sorted({nm for nm, _ in student_entries}, key=str.lower)

    profile_id_by_name: Dict[str, str] = {}
    roster_resolved_keys: set = set()
    try:
        enr = (
            supabase_admin.table("enrollments")
            .select("student_id, profiles(id, full_name)")
            .eq("class_id", class_id)
            .execute()
        )
        enrolled_profiles: List[Dict[str, Any]] = []
        for row in (enr.data or []):
            prof = normalize_profile(row)
            pid = str((prof or {}).get("id") or row.get("student_id") or "").strip()
            fn = str((prof or {}).get("full_name") or "").strip()
            if pid and fn:
                enrolled_profiles.append({"id": pid, "full_name": fn})
        enrolled_index = _enrolled_import_name_index(enrolled_profiles)
        for nm in unique_names:
            hit = _lookup_enrolled_import_student_id(nm, enrolled_index)
            if hit:
                profile_id_by_name[nm.lower()] = hit
                roster_resolved_keys.add(nm.lower())
    except Exception as e:
        logger.error("Import roster name index failed for class %s: %s", class_id, e)

    # Bulk fetch existing profiles for these names.
    if unique_names:
        try:
            CHUNK = 100
            for i in range(0, len(unique_names), CHUNK):
                batch = unique_names[i:i + CHUNK]
                batch = [nm for nm in batch if nm.lower() not in profile_id_by_name]
                if not batch:
                    continue
                resp = (
                    supabase_admin.table("profiles")
                    .select("id, full_name")
                    .in_("full_name", batch)
                    .execute()
                )
                for row in (resp.data or []):
                    fn = (row.get('full_name') or '').strip()
                    pid = row.get('id')
                    if fn and pid and fn.lower() not in profile_id_by_name:
                        profile_id_by_name[fn.lower()] = pid
        except Exception as e:
            logger.error("Bulk profile lookup failed: %s", e)

    # A name match against the global profiles table can coincidentally hit
    # another instructor's student. Only reuse a match with no enrollment in
    # a different class; otherwise drop it so a fresh profile is created
    # below instead of merging into a stranger's roster.
    if profile_id_by_name:
        claimed_elsewhere = _profile_ids_with_enrollment_elsewhere(
            list(profile_id_by_name.values()), class_id
        )
        if claimed_elsewhere:
            profile_id_by_name = {
                name: pid for name, pid in profile_id_by_name.items()
                if name in roster_resolved_keys or pid not in claimed_elsewhere
            }

    # Build missing-profile insert payload (preserve original semantics: new uuid + role=student).
    new_profile_rows: List[Dict[str, Any]] = []
    for nm in unique_names:
        if nm.lower() in profile_id_by_name:
            continue
        new_id = str(uuid4())
        profile_id_by_name[nm.lower()] = new_id
        new_profile_rows.append({
            "id": new_id,
            "full_name": nm,
            "sort_name": _generate_sort_name(nm),
            "role": "student",
        })

    if new_profile_rows:
        try:
            CHUNK = 150
            for i in range(0, len(new_profile_rows), CHUNK):
                supabase_admin.table("profiles").insert(new_profile_rows[i:i + CHUNK]).execute()
        except Exception as e:
            logger.error("Bulk profile insert failed: %s", e)
            # If the bulk insert fails (one bad row poisons the whole chunk in
            # PostgREST), retry one row at a time so a single problematic name
            # is skipped instead of dropping the entire batch. Names that fail
            # are removed from `profile_id_by_name` so subsequent grade rows
            # for them are also skipped, matching the old per-student loop.
            failed: set = set()
            for row in new_profile_rows:
                try:
                    supabase_admin.table("profiles").insert(row).execute()
                except Exception as inner:
                    logger.error("Error creating profile for '%s': %s", row.get('full_name'), inner)
                    failed.add((row.get('full_name') or '').lower())
            for k in failed:
                profile_id_by_name.pop(k, None)

    # Bulk fetch existing enrollments for this class for the resolved profile IDs.
    profile_ids = [pid for pid in profile_id_by_name.values() if pid]
    enrolled_ids: set = set()
    if profile_ids:
        try:
            CHUNK = 200
            for i in range(0, len(profile_ids), CHUNK):
                batch = profile_ids[i:i + CHUNK]
                eresp = (
                    supabase_admin.table("enrollments")
                    .select("student_id")
                    .eq("class_id", class_id)
                    .in_("student_id", batch)
                    .execute()
                )
                for row in (eresp.data or []):
                    sid = row.get('student_id')
                    if sid:
                        enrolled_ids.add(sid)
        except Exception as e:
            logger.error("Bulk enrollment lookup failed: %s", e)

    new_enrollments: List[Dict[str, Any]] = []
    for pid in profile_ids:
        if pid in enrolled_ids:
            continue
        new_enrollments.append({"class_id": class_id, "student_id": pid})
        enrolled_ids.add(pid)

    if new_enrollments:
        try:
            CHUNK = 200
            for i in range(0, len(new_enrollments), CHUNK):
                supabase_admin.table("enrollments").insert(new_enrollments[i:i + CHUNK]).execute()
        except Exception as e:
            logger.error("Bulk enrollment insert failed: %s", e)

    for full_name, student in student_entries:
        profile_id = profile_id_by_name.get(full_name.lower())
        if not profile_id:
            continue

        # Build grade rows to batch-upsert later
        grades = student.get('grades', {}) or {}
        if "homework_pct" in student:
            parsed_hw = Homework.parse_import_hw_pct(student.get("homework_pct"))
            sid_key = str(profile_id).strip()
            if assignment_id and hw_storage_key and parsed_hw is not None:
                hw_rows.append(
                    {
                        "student_id": profile_id,
                        "class_id": class_id,
                        "homework_group": str(hw_storage_key).strip(),
                        "score_pct": parsed_hw,
                    }
                )
                hw_map[sid_key] = parsed_hw
            elif assignment_id and hw_storage_key:
                hw_clear_sids.append(profile_id)
                hw_map.pop(sid_key, None)
        for lo_name, mark in grades.items():
            if not lo_name:
                continue

            lo_id = lo_map.get(lo_name.strip().lower())
            if not lo_id:
                continue

            mark_s = "" if mark is None else str(mark).strip()
            if not mark_s:
                if assignment_id:
                    to_clear_pairs.append(
                        {"student_id": profile_id, "lo_id": lo_id}
                    )
                continue

            normalized = Grade.normalize_score(mark_s)
            if not normalized:
                continue

            if (
                assignment_id
                and normalized != "A"
                and not Homework.student_has_recorded_score(hw_map, profile_id)
            ):
                skipped_hw_warnings.append(
                    f"Skipped: {full_name} - {lo_name} - no HW score recorded"
                )
                continue

            grade_rows.append({
                "student_id": profile_id,
                "learning_objective_id": lo_id,
                "top_score": normalized,
                "assignment_id": assignment_id,
                "last_modified_by": session['user_id'],
            })

        imported += 1

    # Batch upsert grades — overwrites existing scores for the same student+LO+assignment
    try:
        chunk_size = 150
        for i in range(0, len(grade_rows), chunk_size):
            chunk = grade_rows[i:i + chunk_size]
            # Add updated_at to each row so the timestamp refreshes on overwrite
            for row in chunk:
                row["updated_at"] = "now()"
            try:
                supabase_admin.table("grades").upsert(
                    chunk, on_conflict="student_id,learning_objective_id,assignment_id"
                ).execute()
                logger.info("Upserted %d grades", len(chunk))
            except Exception as upsert_err:
                msg = str(upsert_err)
                if "last_modified_by" in msg or "PGRST204" in msg or "schema cache" in msg.lower():
                    # Fallback for deployments that haven't run the last_modified_by migration yet.
                    try:
                        legacy_chunk = [
                            {k: v for k, v in r.items() if k != 'last_modified_by'}
                            for r in chunk
                        ]
                        supabase_admin.table("grades").upsert(
                            legacy_chunk, on_conflict="student_id,learning_objective_id,assignment_id"
                        ).execute()
                        logger.info("Upserted %d grades (legacy, no last_modified_by column)", len(legacy_chunk))
                        continue
                    except Exception as retry_err:
                        upsert_err = retry_err
                logger.error("Grade upsert error: %s", upsert_err)
    except Exception as e:
        logger.error("Error processing grades in bulk: %s", e)

    _log_grade_upserts(grade_rows, str(class_id), session.get("user_id"))

    # Persist homework % scores in homework_scores.
    try:
        if hw_rows:
            chunk_size = 150
            for i in range(0, len(hw_rows), chunk_size):
                chunk = hw_rows[i:i + chunk_size]
                supabase_admin.table("homework_scores").upsert(
                    chunk, on_conflict="student_id,class_id,homework_group"
                ).execute()
            logger.info("Upserted %d homework %% scores from import", len(hw_rows))
    except Exception as e:
        logger.error("Error processing homework scores in bulk: %s", e)

    if assignment_id and to_clear_pairs:
        _clear_grade_cells_for_assignment(
            str(class_id),
            str(assignment_id),
            to_clear_pairs,
            session.get("user_id"),
        )

    try:
        if hw_clear_sids and hw_storage_key:
            uniq_hw = list({str(sid) for sid in hw_clear_sids if sid})
            chunk_size = 150
            for i in range(0, len(uniq_hw), chunk_size):
                batch = uniq_hw[i:i + chunk_size]
                existing_hw = (
                    supabase_admin.table("homework_scores")
                    .select("student_id, class_id, homework_group")
                    .eq("class_id", class_id)
                    .eq("homework_group", str(hw_storage_key).strip())
                    .in_("student_id", batch)
                    .execute()
                )
                for row in (existing_hw.data or []):
                    supabase_admin.table("homework_scores").delete() \
                        .eq("student_id", row["student_id"]) \
                        .eq("class_id", class_id) \
                        .eq("homework_group", str(hw_storage_key).strip()) \
                        .execute()
    except Exception as e:
        logger.error("Error clearing homework scores on import: %s", e)

    return jsonify({
        "success": True,
        "imported_students": imported,
        "imported_grades": len(grade_rows),
        "imported_hw_scores": len(hw_rows),
        "warnings": skipped_hw_warnings,
        "skipped_no_hw": len(skipped_hw_warnings),
    }), 200


@main_bp.route("/api/analyze-grade-pdf", methods=["POST"])
@api_instructor_required
@rate_limited("analyze_grade_pdf", limit=30, window_sec=900)
def analyze_grade_pdf():
    """
    Analyze a grade sheet (PDF or JPG) using Gemini Vision.
    Returns extracted student data and learning objectives.
    Works with both printed and handwritten grade sheets.
    
    Returns:
        JSON with:
        - success: bool
        - data: {students, learning_objectives, raw_text}
        - error: str (if failed)
    """
    try:
        # Check if file is in request
        if 'pdf' not in request.files:
            return jsonify({
                "success": False,
                "error": "No file provided"
            }), 400
        
        pdf_file = request.files['pdf']
        
        if pdf_file.filename == '':
            return jsonify({
                "success": False,
                "error": "No file selected"
            }), 400
        
        # Allow PDF and image files
        allowed_extensions = ('.pdf', '.jpg', '.jpeg', '.png')
        if not pdf_file.filename.lower().endswith(allowed_extensions):
            return jsonify({
                "success": False,
                "error": "File must be a PDF, JPG, or PNG"
            }), 400
        
        # Initialize Gemini Analyzer
        logger.info("Getting Gemini analyzer...")
        analyzer = get_gemini_analyzer()
        logger.info("Analyzer ready: %s", analyzer is not None)
        
        if analyzer is None:
            logger.error(
                "Grade sheet analysis unavailable: GEMINI_API_KEY not configured or google-genai not installed"
            )
            return jsonify({
                "success": False,
                "error": "Grade sheet analysis is not configured."
            }), 500
        
        # Read file content and enforce server-side size/signature checks.
        pdf_content = pdf_file.read()
        if len(pdf_content) > 10 * 1024 * 1024:
            return jsonify({"success": False, "error": "File is too large"}), 400
        if not _allowed_grade_upload_signature(pdf_content, pdf_file.filename):
            return jsonify({"success": False, "error": "File contents do not match an allowed format"}), 400
        
        # Analyze PDF with Gemini
        extracted_data = analyzer.analyze_pdf(pdf_content)
        
        return jsonify({
            "success": True,
            "data": {
                "students": extracted_data.get('students', []),
                "learning_objectives": extracted_data.get('learning_objectives', []),
                "raw_text": extracted_data.get('raw_text', ''),
                "extraction_path": extracted_data.get('extraction_path', 'vision'),
            }
        }), 200
        
    except Exception as e:
        return _safe_api_error("Failed to analyze uploaded file", 500, log_detail=e)


@main_bp.route("/api/class/<class_id>/parse-grade-csv", methods=["POST"])
@api_instructor_required
@rate_limited("parse_grade_csv", limit=30, window_sec=900)
def api_parse_grade_csv(class_id):
    """Parse a Download Blank CSV gradesheet locally. Does not call Gemini."""
    if not _instructor_owns_class(class_id):
        return jsonify({"success": False, "error": "Forbidden"}), 403

    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"success": False, "error": "No file provided"}), 400
    filename = str(upload.filename or "")
    if not filename.lower().endswith(".csv"):
        return jsonify({"success": False, "error": "File must be a CSV"}), 400

    content = upload.read()
    if len(content) > 10 * 1024 * 1024:
        return jsonify({"success": False, "error": "File is too large"}), 400
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return jsonify({
            "success": False,
            "error": "CSV must be UTF-8 encoded (the blank gradesheet export is UTF-8).",
        }), 400

    try:
        los = Course.get_learning_objectives(class_id) or []
    except Exception as e:
        return _safe_api_error("Could not load learning objectives", 500, log_detail=e)
    vendor_codes = [str(lo.get("vendor_code") or "") for lo in los]

    payload, err = parse_blank_gradesheet_csv_text(text, vendor_codes)
    if err:
        return jsonify({"success": False, "error": err}), 400

    assignment_name = payload.get("assignment_name") or ""
    matched_id = None
    try:
        asg_resp = (
            supabase_admin.table("assignments")
            .select("id, name")
            .eq("class_id", class_id)
            .eq("name", assignment_name)
            .execute()
        )
        matches = asg_resp.data or []
        if len(matches) == 1:
            matched_id = matches[0].get("id")
    except Exception as e:
        logger.error("parse-grade-csv assignment lookup failed: %s", e)

    return jsonify({
        "success": True,
        "matched_assignment_id": matched_id,
        "data": {
            "students": payload.get("students") or [],
            "learning_objectives": payload.get("learning_objectives") or [],
            "homework_column": payload.get("homework_column") or "HW",
            "extraction_path": "csv",
            "assignment_name": assignment_name,
            "date_value": payload.get("date_value") or "",
        },
    }), 200
