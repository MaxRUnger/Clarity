import threading
import time
from collections import defaultdict, deque
from functools import wraps

from flask import jsonify, request, session


# In-memory stores below are intentionally per-process. They are documented in
# the module docstring; the brief notes here are reminders for anyone editing
# this file directly. See `create_app` for the MULTI_WORKER startup warning.

# `_rate_limit_events[(scope, identity)] = deque[timestamp]`. A deque per
# bucket avoids re-allocating a list every check and keeps trimming O(1) at
# the head.
_rate_limit_lock = threading.Lock()
_rate_limit_events = defaultdict(deque)


def _client_ip() -> str:
    # ProxyFix (registered in app/__init__.py) already resolves the real
    # client IP into `remote_addr`, trusting exactly one hop (Railway's
    # edge). Reading X-Forwarded-For directly here would let a client set
    # their own value and have it trusted as-is, defeating this rate limiter.
    return (request.remote_addr or "unknown").strip()


_RATE_LIMIT_MAX_IDLE = 3600


def _drop_idle_rate_limit_buckets(now: float) -> None:
    abandoned = [
        key
        for key, events in _rate_limit_events.items()
        if (not events) or (now - events[-1]) > _RATE_LIMIT_MAX_IDLE
    ]
    for key in abandoned:
        del _rate_limit_events[key]


def _rate_limit_bucket(bucket_key: str, limit: int, window_sec: int) -> bool:
    """Record one hit on an arbitrary bucket key. Return False when over the limit."""
    # Idle pruning drops a bucket after _RATE_LIMIT_MAX_IDLE seconds, so every
    # window must be at most that long.
    if window_sec > _RATE_LIMIT_MAX_IDLE:
        raise ValueError("rate limit window exceeds the idle pruning limit")
    now = time.time()
    with _rate_limit_lock:
        dq = _rate_limit_events[bucket_key]
        while dq and (now - dq[0]) > window_sec:
            dq.popleft()
        if len(dq) >= limit:
            _drop_idle_rate_limit_buckets(now)
            return False
        dq.append(now)
        _drop_idle_rate_limit_buckets(now)
        return True


def _rate_limit(key_prefix: str, limit: int, window_sec: int) -> bool:
    key = f"{key_prefix}:{_client_ip()}:{session.get('user_id', 'anon')}"
    return _rate_limit_bucket(key, limit, window_sec)


def rate_limited(key_prefix: str, limit: int, window_sec: int):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not _rate_limit(key_prefix, limit, window_sec):
                return jsonify({"success": False, "error": "Rate limit exceeded. Please retry shortly."}), 429
            return f(*args, **kwargs)
        return wrapped
    return decorator
