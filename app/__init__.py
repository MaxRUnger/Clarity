"""Flask application factory.

`create_app` is intentionally the single entry point so tests can spin up a
fresh app with a custom config without polluting module-level state. Anything
that would otherwise be a global side effect (CORS, blueprint registration,
request hooks, security headers) lives inside this function for that reason.
"""

import logging
import os
import secrets
import hmac
from flask import Flask, session, request, jsonify
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from config import Config

def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)
    
    
    app.secret_key = config_class.SECRET_KEY

    # Railway's edge is the only proxy in front of this app, so trust exactly
    # one X-Forwarded-For / X-Forwarded-Proto hop. Without this, a client can
    # set their own X-Forwarded-For header and have it trusted verbatim by
    # anything that reads request.remote_addr or the header directly (e.g.
    # the rate limiter's _client_ip() in app/routes.py), trivially bypassing
    # per-IP rate limits.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0, x_port=0, x_prefix=0)
    
    # `supports_credentials=True` is required so the browser sends the session
    # cookie on cross-origin XHRs; `allow_headers` includes the custom CSRF
    # header used by the `csrf_protect` before-request hook below.
    CORS(
        app,
        supports_credentials=True,
        origins=app.config.get("CORS_ALLOWED_ORIGINS", []),
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token"],
    )
    
    # Late import: `app.routes` pulls in models / Supabase clients, so we want
    # config to be applied before that import chain runs.
    from app.routes import main_bp, class_display_title
    app.register_blueprint(main_bp)

    # Loud warning at boot: several in-memory stores in `app.routes`
    # (`_pending_mobile_uploads`, `_rate_limit_events`, `_used_form_tokens`)
    # are per-process. Running multiple gunicorn workers without sticky
    # sessions silently breaks them, so we log this once at startup rather
    # than risk debugging it under load.
    if app.config.get("IS_PRODUCTION") and os.environ.get("MULTI_WORKER", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        logging.getLogger(__name__).warning(
            "MULTI_WORKER is set: in-memory mobile upload handoff, rate limits, and "
            "form-token replay tables in app.routes are not shared across workers. "
            "Use one worker, sticky sessions, or Redis. See module docstring in app.routes."
        )

    @app.context_processor
    def inject_instructor_mode():
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return {
            'instructor_mode': session.get('instructor_mode', 'mark'),
            'csrf_token': token,
            'class_display_title': class_display_title,
        }

    @app.before_request
    def slide_idle_session():
        # Asset responses stay free of Set-Cookie. Re-signing every static
        # file would keep an idle tab alive for as long as it kept loading
        # scripts and stylesheets.
        if request.endpoint == "static":
            return None
        if session.get("user_id"):
            session.modified = True
        return None

    @app.before_request
    def csrf_protect():
        # CSRF is only enforced on state-changing methods for authenticated
        # sessions. Unauthenticated POSTs (login/signup) and the mobile-upload
        # endpoints use their own one-time-token schemes and would otherwise
        # 403 before reaching their own validation logic.
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return None
        endpoint = (request.endpoint or "").strip()
        if endpoint in {"main.login", "main.signup"}:
            return None
        if request.path.startswith("/api/mobile-upload/"):
            return None
        if "user_id" not in session:
            return None
        expected = session.get("csrf_token") or ""
        provided = (
            request.headers.get("X-CSRF-Token")
            or request.form.get("csrf_token")
            or ""
        )
        # `hmac.compare_digest` defends against timing side-channels.
        if not expected or not provided or not hmac.compare_digest(str(expected), str(provided)):
            return jsonify({"success": False, "error": "Invalid CSRF token"}), 403
        return None

    @app.after_request
    def add_security_headers(resp):
        # CSP is in report-only mode while the codebase still ships inline
        # event handlers and CDN scripts; flipping to enforcing mode requires
        # auditing every `onclick=` / inline `<script>` first.
        resp.headers.setdefault("Content-Security-Policy-Report-Only", "default-src 'self' https: data: blob: 'unsafe-inline' 'unsafe-eval'; img-src 'self' https: data: blob:;")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if app.config.get("IS_PRODUCTION"):
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains; preload")
        # The CSRF token is also mirrored into an `XSRF-TOKEN` cookie so
        # client-side fetch wrappers can read it (the session cookie itself
        # remains HttpOnly). `httponly=False` is intentional here.
        if request.endpoint != "static":
            token = session.get("csrf_token")
            if token:
                resp.set_cookie(
                    "XSRF-TOKEN",
                    token,
                    secure=app.config.get("SESSION_COOKIE_SECURE", False),
                    samesite=app.config.get("SESSION_COOKIE_SAMESITE", "Lax"),
                    httponly=False,
                )
        return resp
    
    return app