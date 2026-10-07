import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str, default: str = ""):
    raw = os.environ.get(name, default)
    return [v.strip() for v in str(raw).split(",") if v.strip()]


def _running_on_railway() -> bool:
    """True when the process is actually executing on Railway's infrastructure,
    independent of whatever APP_ENV happens to be set to. Railway injects these
    three unconditionally on every build/deployment (see
    https://docs.railway.com/variables/reference) so they're a far more
    reliable "is this a real deployment" signal than a manually-set env var
    that can be forgotten, misspelled, or dropped on a redeploy.
    """
    return any(
        os.environ.get(name)
        for name in ("RAILWAY_ENVIRONMENT_ID", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID")
    )


class Config:
    # Empty (not "development") when unset, so we can tell "never configured"
    # apart from someone explicitly opting out of production hardening below.
    APP_ENV = os.environ.get("APP_ENV", "").strip().lower()

    # Production hardening (secure cookies, HSTS, strict SECRET_KEY) applies
    # whenever APP_ENV explicitly says so, OR whenever we detect we're
    # actually running on Railway. The Railway check is a safety net: if a
    # deploy ever ships without APP_ENV set, we still lock down rather than
    # silently boot with development defaults on a public deployment. To
    # intentionally run relaxed settings on a Railway-hosted staging/preview
    # environment, set APP_ENV to one of the values below to opt out.
    _RELAXED_APP_ENVS = ("development", "dev", "local", "staging", "test", "testing")
    IS_PRODUCTION = APP_ENV in ("prod", "production") or (
        _running_on_railway() and APP_ENV not in _RELAXED_APP_ENVS
    )

    SECRET_KEY = os.environ.get("SECRET_KEY")
    if IS_PRODUCTION and not SECRET_KEY:
        raise RuntimeError("SECRET_KEY is required in production.")
    if not SECRET_KEY:
        # Development fallback only.
        SECRET_KEY = "dev-secret-key-local-only"

    # If set, signup requires this code. Leave unset to allow open signup.
    SIGNUP_INVITE_CODE = os.environ.get("SIGNUP_INVITE_CODE")

    # Session cookie hardening.
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", IS_PRODUCTION)
    SESSION_COOKIE_HTTPONLY = _env_bool("SESSION_COOKIE_HTTPONLY", True)
    SESSION_COOKIE_SAMESITE = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax")
    SESSION_COOKIE_NAME = os.environ.get("SESSION_COOKIE_NAME", "clarity_session")
    PERMANENT_SESSION_LIFETIME = timedelta(
        minutes=int(os.environ.get("SESSION_LIFETIME_MINUTES", "480"))
    )
    SESSION_REFRESH_EACH_REQUEST = _env_bool("SESSION_REFRESH_EACH_REQUEST", True)

    # CORS strict allowlist (credentials-enabled).
    # Railway / prod must set this explicitly, e.g. https://app.example.com --
    # otherwise a production deploy would silently fall back to a localhost-only
    # allowlist with no warning, breaking every legitimate cross-origin request
    # while giving no signal anything is wrong. Fail loudly instead, matching
    # the SECRET_KEY pattern above (H-A).
    _cors_env_raw = os.environ.get("CORS_ALLOWED_ORIGINS")
    if IS_PRODUCTION and not (_cors_env_raw or "").strip():
        raise RuntimeError(
            "CORS_ALLOWED_ORIGINS is required in production (comma-separated "
            "list of allowed origins, e.g. https://app.example.com)."
        )
    CORS_ALLOWED_ORIGINS = _env_list(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:5000,http://127.0.0.1:5000",
    )