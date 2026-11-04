import threading
import time


# Single-use form tokens to block replay of state-changing POSTs that have
# already been processed (e.g. double-click submit).
FORM_TOKEN_TTL = 900  # 15 minutes
_used_form_tokens = {}
_form_token_lock = threading.Lock()


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
