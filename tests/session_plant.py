"""Plant a cookie token and the matching app_sessions row for client tests."""
import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

PLANTED = {}


def session_row(token, user_id):
    return {
        "token_hash": hashlib.sha256(str(token).encode("utf-8")).hexdigest(),
        "user_id": str(user_id),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat(),
        "revoked_at": None,
    }


def plant_session(
    client,
    user_id="u1",
    role="instructor",
    csrf_token="tok",
    session_token="test-session-token",
    when=None,
    **extra,
):
    def fill(sess):
        sess["user_id"] = user_id
        sess["role"] = role
        sess["csrf_token"] = csrf_token
        sess["session_token"] = session_token
        sess.update(extra)

    if when is None:
        with client.session_transaction() as sess:
            fill(sess)
    else:
        with patch("itsdangerous.timed.time.time", return_value=when):
            with client.session_transaction() as sess:
                fill(sess)
    row = session_row(session_token, user_id)
    PLANTED[row["token_hash"]] = row
    return session_token


class _SessionQuery:
    def __init__(self, planted):
        self.planted = planted
        self.filters = []
        self.payload = None
        self.op = "select"

    def select(self, *_args, **_kwargs):
        self.op = "select"
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def limit(self, _count):
        return self

    def update(self, payload):
        self.op = "update"
        self.payload = payload
        return self

    def insert(self, payload):
        self.op = "insert"
        self.payload = payload
        return self

    def delete(self):
        self.op = "delete"
        return self

    def lt(self, _col, _val):
        return self

    def execute(self):
        if self.op == "update":
            for row in self.planted.values():
                if all(row.get(col) == val for col, val in self.filters):
                    row.update(self.payload)
        matched = [
            dict(row)
            for row in self.planted.values()
            if all(row.get(col) == val for col, val in self.filters)
        ]
        result = MagicMock()
        result.data = matched if self.op == "select" else []
        return result


def serving_admin():
    admin = MagicMock()

    def table(name):
        if name == "app_sessions":
            return _SessionQuery(PLANTED)
        chain = MagicMock()
        for method in (
            "select", "eq", "in_", "limit", "order", "insert", "update",
            "delete", "single", "range", "lt", "gte", "is_",
        ):
            getattr(chain, method).return_value = chain
        chain.execute.return_value.data = []
        chain.execute.return_value.count = 0
        return chain

    admin.table.side_effect = table
    return admin


def install_app_session(admin, token, user_id):
    row = session_row(token, user_id)
    PLANTED[row["token_hash"]] = row
    previous_side = admin.table.side_effect
    previous_return = admin.table.return_value

    def table(name, *args, **kwargs):
        if name == "app_sessions":
            return _SessionQuery(PLANTED)
        if previous_side is not None:
            return previous_side(name, *args, **kwargs)
        return previous_return

    admin.table.side_effect = table
    return row
