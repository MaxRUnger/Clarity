"""Signup accepts an invite only when signup_invites says the code is still usable."""
import logging
import os
import unittest
from unittest.mock import MagicMock, patch

from app import create_app

_ABC_HASH = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
_MESSAGE = "Invalid invite code."


def _invite_row(**overrides):
    row = {
        "revoked_at": None,
        "expires_at": None,
        "used_count": 0,
        "max_uses": 1,
    }
    row.update(overrides)
    return row


class TestSignupInvites(unittest.TestCase):
    def setUp(self):
        from app.routes import _rate_limit_events

        _rate_limit_events.clear()
        app = create_app()
        app.config["TESTING"] = True
        self.client = app.test_client()

    def _post(self, code, rows=None, boom=None, invite_env=""):
        from app.routes import _rate_limit_events

        _rate_limit_events.clear()
        shared = MagicMock()
        shared.auth.sign_up.return_value = MagicMock(user=MagicMock())
        admin = MagicMock()
        query = (
            admin.table.return_value.select.return_value.eq.return_value.limit.return_value
        )
        if boom is not None:
            query.execute.side_effect = boom
        else:
            query.execute.return_value = MagicMock(data=[] if rows is None else rows)
        payload = {
            "name": "Ada Lovelace",
            "email": "ada@example.com",
            "password": "Correct-horse1!",
            "invite_code": code,
        }
        with patch("app.routes.supabase", shared        ), patch(
            "app.invites.supabase_admin", admin
        ), patch(
            "app.routes.Config.PUBLIC_BASE_URL",
            "https://clarity.example",
            create=True,
        ), patch(
            "app.routes.Config.SIGNUP_INVITE_CODE",
            invite_env,
            create=True,
        ), patch.dict(
            os.environ, {"SIGNUP_INVITE_CODE": invite_env}, clear=False
        ):
            response = self.client.post("/api/signup", json=payload)
        return response, shared.auth.sign_up, admin

    def _assert_rejected(self, code, rows=None, boom=None, invite_env=""):
        response, sign_up, admin = self._post(
            code, rows=rows, boom=boom, invite_env=invite_env
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["message"], _MESSAGE)
        sign_up.assert_not_called()
        return admin

    def test_unknown_code_is_rejected(self):
        self._assert_rejected("abc", rows=[])

    def test_empty_code_is_rejected_without_a_query(self):
        admin = self._assert_rejected("")
        admin.table.assert_not_called()

    def test_revoked_code_is_rejected(self):
        self._assert_rejected(
            "abc",
            rows=[_invite_row(revoked_at="2020-01-01T00:00:00+00:00")],
        )

    def test_expired_code_is_rejected(self):
        self._assert_rejected(
            "abc",
            rows=[_invite_row(expires_at="2000-01-01T00:00:00+00:00")],
        )

    def test_exhausted_code_is_rejected(self):
        self._assert_rejected(
            "abc",
            rows=[_invite_row(used_count=2, max_uses=2)],
        )

    def test_lookup_error_is_rejected_and_the_code_is_not_logged(self):
        code = "do-not-log-this"
        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Capture()
        handler.setLevel(logging.ERROR)
        logger = logging.getLogger("app.invites")
        logger.addHandler(handler)
        try:
            self._assert_rejected(code, boom=RuntimeError(code))
        finally:
            logger.removeHandler(handler)
        rendered = " ".join(record.getMessage() for record in records)
        self.assertNotIn(code, rendered)
        self.assertIn("RuntimeError", rendered)

    def test_valid_code_is_sent_to_sign_up(self):
        response, sign_up, admin = self._post(
            "abc",
            rows=[_invite_row(expires_at="2099-06-01T00:00:00+00:00", max_uses=5)],
        )
        self.assertEqual(response.status_code, 200)
        sign_up.assert_called_once()
        body = sign_up.call_args.args[0]
        self.assertEqual(body["options"]["data"]["invite_code"], "abc")
        admin.table.assert_called_with("signup_invites")
        admin.table.return_value.select.return_value.eq.assert_called_with(
            "code_hash", _ABC_HASH
        )
        admin.table.return_value.select.return_value.eq.return_value.limit.assert_called_with(
            1
        )

    def test_signup_ignores_the_invite_environment_variable(self):
        response, sign_up, _admin = self._post(
            "abc",
            rows=[_invite_row()],
            invite_env="not-the-db-code",
        )
        self.assertEqual(response.status_code, 200)
        sign_up.assert_called_once()
        body = sign_up.call_args.args[0]
        self.assertEqual(body["options"]["data"]["invite_code"], "abc")

    def test_invite_code_hash_of_abc(self):
        from app.invites import invite_code_hash

        self.assertEqual(invite_code_hash("abc"), _ABC_HASH)
        self.assertEqual(invite_code_hash("  abc"), _ABC_HASH)
