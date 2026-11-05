import hashlib
import logging
import secrets
import time
from urllib.parse import quote
from flask import render_template, request, jsonify, redirect, url_for, session
from app.helpers.authentication import supabase, supabase_admin
from app.helpers.account_mail import (
    _forgot_ok,
    _profile_role,
    _public_base_url,
    _send_account_email,
    verify_recovery_token,
)
from app.helpers.passwords import password_error
from app.helpers.invites import invite_code_is_valid
from app.helpers.student_names import normalize_email
from app.helpers.auth_decorators import _session_expired_response, api_login_required
from app.helpers.api_input import _require_json_object, _safe_api_error
from app.helpers.session_status import _session_seconds_left, _session_status_response
from app.helpers.rate_limit import rate_limited, _rate_limit, _rate_limit_bucket
from app.routes.blueprint import main_bp

logger = logging.getLogger(__name__)

_INSTRUCTOR_HOME = "/instructor/dashboard"

@main_bp.route("/")
@main_bp.route("/login")
def login_page():
    return render_template("auth/login.html")

@main_bp.route("/signup")
def signup_page():
    return render_template("auth/signup.html")

@main_bp.route("/forgot-password")
def forgot_password_page():
    return render_template("auth/forgot_password.html")

@main_bp.route("/reset-password")
def reset_password_page():
    return render_template("auth/reset_password.html")

@main_bp.route("/logout", methods=["POST"])
def logout():
    from app.helpers.app_sessions import revoke_live_session
    reason = request.form.get("reason")
    try:
        revoke_live_session(session.get("session_token"), session.get("user_id"))
    except Exception as exc:
        logger.error(
            "app_sessions revoke failed user_id=%s: %s",
            session.get("user_id"),
            exc,
        )
    session.clear()
    if reason == "inactivity":
        return redirect(url_for("main.login_page", reason="inactivity"))
    return redirect(url_for("main.login_page"))

@main_bp.route("/api/session-status")
def session_status():
    if "user_id" not in session:
        return _session_status_response({
            "success": False,
            "error": "Unauthorized",
            "code": "session_expired",
        }, 401)
    remaining = _session_seconds_left()
    if remaining is None:
        return _session_status_response({
            "success": False,
            "error": "Unauthorized",
            "code": "session_expired",
        }, 401)
    return _session_status_response({"ok": True, "seconds_left": remaining}, 200)

@main_bp.route("/api/session-keepalive", methods=["POST"])
def session_keepalive():
    if "user_id" not in session:
        return _session_expired_response()
    return jsonify({"ok": True})

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
            try:
                prof_resp = (
                    supabase_admin.table("profiles")
                    .select("role")
                    .eq("id", user_id)
                    .limit(1)
                    .execute()
                )
            except Exception as e:
                logger.error("profiles.role lookup failed for %s: %s", user_id, e)
                return jsonify({"success": False, "message": "Invalid credentials"}), 401

            role = ""
            if prof_resp.data:
                role = (prof_resp.data[0].get("role") or "").strip().lower()
            if role != "instructor":
                return jsonify({"success": False, "message": "Invalid credentials"}), 401

            from app.helpers.app_sessions import (
                absolute_expiry,
                delete_stale_sessions,
                new_session_token,
                remember_app_session,
            )
            token = new_session_token()
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            expires_at = absolute_expiry()
            supabase_admin.table("app_sessions").insert({
                "token_hash": token_hash,
                "user_id": user_id,
                "expires_at": expires_at.isoformat(),
            }).execute()
            remember_app_session(token_hash, user_id, expires_at, time.monotonic())
            session.clear()
            session['user_id'] = user_id
            session['role'] = role
            session['full_name'] = result.user.user_metadata.get('full_name', '')
            session['csrf_token'] = secrets.token_urlsafe(32)
            session['session_token'] = token
            try:
                delete_stale_sessions()
            except Exception as exc:
                logger.error(
                    "app_sessions cleanup failed user_id=%s: %s",
                    user_id,
                    exc,
                )
            return jsonify({"success": True, "redirect": _INSTRUCTOR_HOME})
        return jsonify({"success": False, "message": "Invalid credentials"})
    except Exception as e:
        return _safe_api_error("Invalid credentials", 401, log_detail=e)

@main_bp.route("/api/signup", methods=["POST"])
@rate_limited("api_signup", limit=10, window_sec=300)
def signup():
    data = request.get_json()
    try:
        submitted = (data.get("invite_code") or "").strip()
        if not invite_code_is_valid(submitted):
            return jsonify({"success": False, "message": "Invalid invite code."}), 403

        signup_name = (data.get("name") or "").strip()
        if len(signup_name) > 255:
            return jsonify({"success": False, "message": "Name must be 255 characters or fewer."}), 400

        login_redirect_url = _public_base_url()
        if not login_redirect_url:
            return jsonify({"success": False, "message": "Signup is not available."}), 403
        problem = password_error(data.get("password"), data.get("email"))
        if problem:
            return jsonify({"success": False, "message": problem}), 400
        login_redirect_url = f"{login_redirect_url}/login"
        result = supabase.auth.sign_up({
            "email": data.get("email"),
            "password": data.get("password"),
            "options": {
                "email_redirect_to": login_redirect_url,
                "data": {
                    "full_name": data.get("name"),
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

@main_bp.route("/api/forgot-password", methods=["POST"])
def forgot_password():
    data, error = _require_json_object()
    if error:
        return error
    if not _rate_limit("forgot_password_ip", 10, 900):
        return jsonify({"success": False, "error": "Rate limit exceeded. Please retry shortly."}), 429
    email = normalize_email((data or {}).get("email"))
    if not email:
        return jsonify({"success": False, "message": "Enter a valid email address."}), 400
    if not _rate_limit_bucket(f"forgot_password_email:{email}", 5, 3600):
        return jsonify({"success": False, "error": "Rate limit exceeded. Please retry shortly."}), 429
    base = _public_base_url()
    if not base:
        return jsonify({"success": False, "message": "Password reset is not available."}), 403
    try:
        link = supabase_admin.auth.admin.generate_link({
            "type": "recovery",
            "email": email,
            "options": {"redirect_to": f"{base}/reset-password"},
        })
    except Exception as exc:
        logger.error("Password reset link failed: %s", type(exc).__name__)
        return _forgot_ok()
    user = getattr(link, "user", None)
    user_id = getattr(user, "id", None) if user is not None else None
    properties = getattr(link, "properties", None)
    hashed = getattr(properties, "hashed_token", None) if properties is not None else None
    if not user_id or not hashed:
        return _forgot_ok()
    if _profile_role(user_id) != "instructor":
        return _forgot_ok()
    if not _rate_limit_bucket(f"forgot_password_account:{user_id}", 3, 3600):
        return _forgot_ok()
    token = quote(str(hashed), safe="")
    mailed = quote(email, safe="")
    url = f"{base}/reset-password#token_hash={token}&type=recovery&email={mailed}"
    _send_account_email(
        email,
        "Reset your Clarity Grader password",
        "Use this link to choose a new Clarity Grader password. It expires in one hour.\n\n"
        f"{url}\n\n"
        "If you did not ask for this, you can ignore this message.\n",
    )
    return _forgot_ok()

@main_bp.route("/api/reset-password", methods=["POST"])
def reset_password():
    data, error = _require_json_object()
    if error:
        return error
    email = normalize_email((data or {}).get("email"))
    token_hash = str((data or {}).get("token_hash") or "").strip()
    password = (data or {}).get("password")
    if not email or not token_hash or not isinstance(password, str):
        return jsonify({"success": False, "message": "This reset link is invalid or expired."}), 400
    problem = password_error(password, email)
    if problem:
        return jsonify({"success": False, "message": problem}), 400
    if not _rate_limit("reset_password_ip", 10, 900):
        return jsonify({"success": False, "error": "Rate limit exceeded. Please retry shortly."}), 429
    try:
        verified = verify_recovery_token(token_hash)
    except Exception as exc:
        logger.error("Password reset verify failed: %s", type(exc).__name__)
        return jsonify({"success": False, "message": "This reset link is invalid or expired."}), 400
    user = getattr(verified, "user", None)
    user_id = getattr(user, "id", None) if user is not None else None
    verified_email = normalize_email(
        getattr(user, "email", None) if user is not None else None
    )
    if not user_id or verified_email != email or _profile_role(user_id) != "instructor":
        return jsonify({"success": False, "message": "This reset link is invalid or expired."}), 400
    try:
        from app.helpers.app_sessions import revoke_user_sessions
        revoke_user_sessions(user_id)
    except Exception as exc:
        logger.error("Password reset session revoke failed: %s", type(exc).__name__)
        return jsonify({
            "success": False,
            "message": "We couldn't save the new password. Request a new reset link.",
        }), 500
    try:
        supabase_admin.auth.admin.update_user_by_id(user_id, {"password": password})
    except Exception as exc:
        logger.error("Password reset update failed: %s", type(exc).__name__)
        return jsonify({
            "success": False,
            "message": "We couldn't save the new password. Request a new reset link.",
        }), 400
    sent = _send_account_email(
        email,
        "Your Clarity Grader password was changed",
        "Your Clarity Grader password was changed.\n\n"
        "If you did not make this change, contact the site owner.\n",
    )
    if not sent:
        logger.error("Password changed notice failed")
    return jsonify({"success": True, "redirect": "/login?reset=1"})

