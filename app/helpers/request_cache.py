from typing import Any, Dict

from flask import g


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
