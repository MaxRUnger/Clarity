"""Give client tests a live app_sessions row when they sign a cookie in."""
import pytest

from app.app_sessions import clear_app_session_cache
from tests.session_plant import PLANTED, session_row

_AUTO = set()


@pytest.fixture(autouse=True)
def planted_app_sessions(monkeypatch):
    clear_app_session_cache()
    PLANTED.clear()
    _AUTO.clear()

    import app.app_sessions as sessions

    real_live = sessions._token_is_live
    real_fetch = sessions._fetch_row

    def token_is_live(token, user_id):
        if user_id and not token:
            token = "planted-%s" % user_id
            row = session_row(token, user_id)
            PLANTED.setdefault(row["token_hash"], row)
            _AUTO.add(row["token_hash"])
        return real_live(token, user_id)

    def fetch(token_hash):
        if token_hash in _AUTO:
            return dict(PLANTED[token_hash])
        return real_fetch(token_hash)

    monkeypatch.setattr(sessions, "_token_is_live", token_is_live)
    monkeypatch.setattr(sessions, "_fetch_row", fetch)
    yield
    clear_app_session_cache()
    PLANTED.clear()
    _AUTO.clear()
