"""Server-side session records, logout, and cookie boot checks."""
import hashlib
import importlib
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from flask import jsonify, session

from app import create_app


def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _iso(when):
    return when.astimezone(timezone.utc).isoformat()


class _Table:
    def __init__(self, name, db):
        self.name = name
        self.db = db
        self.filters = []
        self.op = "select"
        self.payload = None

    def select(self, *_args, **_kwargs):
        self.op = "select"
        self.db.setdefault("selects", []).append(self.name)
        return self

    def insert(self, payload):
        self.op = "insert"
        self.payload = payload
        self.db["ops"].append(("insert", self.name, payload))
        return self

    def update(self, payload):
        self.op = "update"
        self.payload = payload
        self.db["ops"].append(("update", self.name, payload))
        return self

    def delete(self):
        self.op = "delete"
        self.db["ops"].append(("delete", self.name, None))
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def limit(self, _n):
        return self

    def or_(self, expr):
        self.db["ors"].append(expr)
        return self

    def lt(self, col, val):
        self.db.setdefault("lts", []).append((self.name, col, val))
        return self

    def is_(self, _col, _val):
        return self

    def execute(self):
        if self.db.get("boom") == self.name and self.op == "select":
            raise RuntimeError(self.db.get("boom_message") or "db down")
        if self.op == "delete" and self.db.get("delete_boom"):
            raise RuntimeError(self.db["delete_boom"])
        if self.name == "profiles" and self.op == "select":
            return MagicMock(data=[{"role": "instructor"}])
        if self.name == "app_sessions" and self.op == "select":
            rows = []
            for row in self.db.get("sessions", []):
                if all(row.get(col) == val for col, val in self.filters):
                    rows.append(dict(row))
            return MagicMock(data=rows)
        if self.name == "app_sessions" and self.op == "update":
            for row in self.db.get("sessions", []):
                if all(row.get(col) == val for col, val in self.filters):
                    row.update(self.payload)
            return MagicMock(data=[])
        return MagicMock(data=[{"id": "new-class"}])


def _admin(db):
    admin = MagicMock()
    admin.table.side_effect = lambda name: _Table(name, db)
    return admin


def _patch_admins(admin):
    return patch.multiple(
        "app.routes",
        supabase_admin=admin,
    )


class TestAppSessionOrder(unittest.TestCase):
    def test_validation_is_registered_before_slide_and_csrf(self):
        app = create_app()
        names = [fn.__name__ for fn in app.before_request_funcs[None]]
        relevant = [
            name for name in names
            if name in {"validate_app_session", "slide_idle_session", "csrf_protect"}
        ]
        self.assertEqual(
            relevant,
            ["validate_app_session", "slide_idle_session", "csrf_protect"],
        )

    def test_refused_cookie_is_cleared_before_csrf(self):
        app = create_app()
        app.config["TESTING"] = True

        @app.route("/_probe")
        def _probe():
            return jsonify({"user_id": session.get("user_id")})

        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = "user-1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "tok"
            sess["session_token"] = "absent-token"
        empty = MagicMock()
        empty.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
        with patch("app.routes.supabase_admin", empty):
            denied = client.post("/api/session-keepalive")
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.get_json()["code"], "session_expired")
        self.assertNotEqual(denied.get_json().get("error"), "Invalid CSRF token")
        with client.session_transaction() as sess:
            self.assertNotIn("user_id", sess)


class TestAppSessionCache(unittest.TestCase):
    def test_cache_limit_is_500_and_add_drops_expired_entries(self):
        from app.app_sessions import (
            CACHE_LIMIT,
            CACHE_SECONDS,
            app_session_cache_size,
            cached_app_session,
            clear_app_session_cache,
            remember_app_session,
        )

        self.assertEqual(CACHE_LIMIT, 500)
        self.assertEqual(CACHE_SECONDS, 30)
        clear_app_session_cache()
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        remember_app_session("old", "user-1", future, now=0)
        remember_app_session("new", "user-1", future, now=31)
        self.assertEqual(app_session_cache_size(), 1)
        self.assertIsNone(cached_app_session("old", now=31))
        self.assertEqual(cached_app_session("new", now=31)[0], "user-1")

    def test_cache_holds_at_most_500_entries(self):
        from app.app_sessions import (
            app_session_cache_size,
            cached_app_session,
            clear_app_session_cache,
            remember_app_session,
        )

        clear_app_session_cache()
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        for index in range(500):
            remember_app_session("h%d" % index, "user-1", future, now=1000 + (index * 0.01))
        self.assertEqual(app_session_cache_size(), 500)
        remember_app_session("extra", "user-1", future, now=1005)
        self.assertEqual(app_session_cache_size(), 500)
        self.assertIsNone(cached_app_session("h0", now=1005))
        self.assertEqual(cached_app_session("extra", now=1005)[0], "user-1")

    def test_cached_expires_at_is_enforced_on_every_hit(self):
        from app.app_sessions import (
            cached_app_session,
            clear_app_session_cache,
            remember_app_session,
        )

        clear_app_session_cache()
        expires = datetime.now(timezone.utc) + timedelta(seconds=5)
        remember_app_session("h", "user-1", expires, now=50)
        self.assertEqual(cached_app_session("h", now=51)[0], "user-1")

        class Frozen:
            @classmethod
            def now(cls, _tz=None):
                return expires + timedelta(seconds=1)

        with patch("app.app_sessions.datetime", Frozen):
            self.assertIsNone(cached_app_session("h", now=52))


class TestLogoutPost(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True

        @self.app.route("/_probe")
        def _probe():
            return jsonify({"user_id": session.get("user_id")})

        self.client = self.app.test_client()
        self.db = {"ops": [], "ors": [], "sessions": []}

    def _plant(self, token="sess-token"):
        with self.client.session_transaction() as sess:
            sess["user_id"] = "user-1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "tok"
            sess["full_name"] = "Ada"
            sess["session_token"] = token

    def _row(self, token="sess-token", *, expires_at=None, revoked_at=None):
        if expires_at is None:
            expires_at = _iso(datetime.now(timezone.utc) + timedelta(hours=12))
        return {
            "token_hash": _hash(token),
            "user_id": "user-1",
            "expires_at": expires_at,
            "revoked_at": revoked_at,
        }

    def _post(self, reason="inactivity"):
        admin = _admin(self.db)
        with patch("app.routes.supabase_admin", admin), \
             patch("app.authentication.supabase_admin", admin):
            return self.client.post(
                "/logout",
                data={"csrf_token": "tok", "reason": reason},
            )

    def _session_cookies(self, response):
        return [
            cookie for cookie in response.headers.getlist("Set-Cookie")
            if cookie.lower().startswith("clarity_session=")
        ]

    def test_get_logout_leaves_the_session(self):
        self._plant()
        response = self.client.get("/logout")
        self.assertEqual(response.status_code, 405)
        cleared = [cookie for cookie in self._session_cookies(response) if "max-age=0" in cookie.lower()]
        self.assertEqual(cleared, [])
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("user_id"), "user-1")

    def test_post_logout_revokes_a_live_row_and_replay_is_rejected(self):
        self._plant()
        self.db["sessions"] = [self._row()]
        response = self._post(reason="nope")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))
        self.assertNotIn("reason=", response.headers["Location"])
        self.assertTrue(any("max-age=0" in cookie.lower() for cookie in self._session_cookies(response)))
        updates = [op for op in self.db["ops"] if op[0] == "update" and op[1] == "app_sessions"]
        self.assertEqual(len(updates), 1)
        self.assertIsNotNone(updates[0][2].get("revoked_at"))
        with self.client.session_transaction() as sess:
            sess["user_id"] = "user-1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "tok"
            sess["session_token"] = "sess-token"
        admin = _admin(self.db)
        with patch("app.routes.supabase_admin", admin), \
             patch("app.authentication.supabase_admin", admin):
            replay = self.client.get("/_probe")
        self.assertEqual(replay.status_code, 302)
        self.assertTrue(replay.headers["Location"].endswith("/login"))

    def test_post_logout_of_revoked_or_expired_cookie_redirects_and_clears(self):
        for revoked_at, expires_at in (
            (_iso(datetime.now(timezone.utc) - timedelta(minutes=5)), _iso(datetime.now(timezone.utc) + timedelta(hours=1))),
            (None, _iso(datetime.now(timezone.utc) - timedelta(minutes=5))),
        ):
            self.db = {"ops": [], "ors": [], "sessions": [self._row(revoked_at=revoked_at, expires_at=expires_at)]}
            self._plant()
            response = self._post(reason="inactivity")
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers["Location"].endswith("/login?reason=inactivity"))
            self.assertTrue(any("max-age=0" in cookie.lower() for cookie in self._session_cookies(response)))
            updates = [op for op in self.db["ops"] if op[0] == "update"]
            self.assertEqual(updates, [])
            with self.client.session_transaction() as sess:
                self.assertNotIn("user_id", sess)

    def test_post_logout_without_csrf_returns_403_and_keeps_the_session(self):
        self._plant()
        self.db["sessions"] = [self._row()]
        admin = _admin(self.db)
        with patch("app.routes.supabase_admin", admin), \
             patch("app.authentication.supabase_admin", admin):
            response = self.client.post("/logout", data={"reason": "inactivity"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "Invalid CSRF token")
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("user_id"), "user-1")

    def test_other_reason_values_are_ignored(self):
        self._plant()
        self.db["sessions"] = [self._row()]
        response = self._post(reason="please")
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("reason=", response.headers["Location"])


class TestSessionLookup(unittest.TestCase):
    """A missing app_sessions row stands in for ON DELETE CASCADE after a profile delete."""

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True

        @self.app.route("/_probe")
        def _probe():
            return jsonify({"user_id": session.get("user_id")})

        self.client = self.app.test_client()

    def _plant(self):
        with self.client.session_transaction() as sess:
            sess["user_id"] = "user-1"
            sess["role"] = "instructor"
            sess["csrf_token"] = "tok"
            sess["session_token"] = "sess-token"

    def test_missing_row_after_profile_cascade_is_refused(self):
        self._plant()
        db = {"ops": [], "ors": [], "sessions": []}
        admin = _admin(db)
        with patch("app.routes.supabase_admin", admin), \
             patch("app.authentication.supabase_admin", admin):
            response = self.client.get("/_probe")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))
        with self.client.session_transaction() as sess:
            self.assertNotIn("user_id", sess)

    def test_expired_row_is_refused(self):
        self._plant()
        db = {"ops": [], "ors": [], "sessions": [{
            "token_hash": _hash("sess-token"),
            "user_id": "user-1",
            "expires_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=1)),
            "revoked_at": None,
        }]}
        admin = _admin(db)
        with patch("app.routes.supabase_admin", admin), \
             patch("app.authentication.supabase_admin", admin):
            response = self.client.get("/_probe")
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as sess:
            self.assertNotIn("user_id", sess)

    def test_lookup_error_fails_closed(self):
        import logging
        self._plant()
        db = {
            "ops": [],
            "ors": [],
            "sessions": [],
            "boom": "app_sessions",
            "boom_message": "session lookup exploded",
        }
        admin = _admin(db)
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage())
        logger = logging.getLogger("app.app_sessions")
        logger.addHandler(handler)
        try:
            with patch("app.routes.supabase_admin", admin), \
                 patch("app.authentication.supabase_admin", admin):
                page = self.client.get("/_probe")
                with self.client.session_transaction() as sess:
                    sess["user_id"] = "user-1"
                    sess["role"] = "instructor"
                    sess["csrf_token"] = "tok"
                    sess["session_token"] = "sess-token"
                status = self.client.get("/api/session-status")
        finally:
            logger.removeHandler(handler)
        self.assertEqual(page.status_code, 302)
        self.assertTrue(page.headers["Location"].endswith("/login"))
        self.assertEqual(status.status_code, 401)
        self.assertEqual(status.get_json()["code"], "session_expired")
        self.assertTrue(any("user-1" in message and "session lookup exploded" in message for message in records))

    def test_revoked_row_is_refused_after_the_cache_window(self):
        from app.app_sessions import clear_app_session_cache

        clear_app_session_cache()
        self._plant()
        db = {"ops": [], "ors": [], "sessions": [{
            "token_hash": _hash("sess-token"),
            "user_id": "user-1",
            "expires_at": _iso(datetime.now(timezone.utc) + timedelta(hours=12)),
            "revoked_at": None,
        }]}
        clock = {"now": 1000}

        def monotonic():
            return clock["now"]

        admin = _admin(db)
        with patch("app.app_sessions._clock", side_effect=monotonic), \
             patch("app.routes.supabase_admin", admin), \
             patch("app.authentication.supabase_admin", admin):
            first = self.client.get("/_probe")
            self.assertEqual(first.status_code, 200)
            db["sessions"][0]["revoked_at"] = _iso(datetime.now(timezone.utc))
            clock["now"] = 1001
            during = self.client.get("/_probe")
            self.assertEqual(during.status_code, 200)
            clock["now"] = 1031
            after = self.client.get("/_probe")
        self.assertEqual(after.status_code, 302)
        self.assertTrue(after.headers["Location"].endswith("/login"))


class TestLoginSessionRow(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def _login(self, db):
        user = MagicMock()
        user.id = "user-1"
        user.user_metadata = {"full_name": "Ada"}
        auth = MagicMock()
        auth.auth.sign_in_with_password.return_value = MagicMock(user=user)
        admin = _admin(db)
        with patch("app.routes.supabase", auth), \
             patch("app.routes.supabase_admin", admin), \
             patch("app.authentication.supabase_admin", admin):
            response = self.client.post(
                "/api/login",
                json={"email": "ada@school.edu", "password": "secret"},
            )
        return response

    def test_login_inserts_a_row_then_deletes_rows_older_than_seven_days(self):
        db = {"ops": [], "ors": [], "sessions": []}
        response = self._login(db)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [(op[0], op[1]) for op in db["ops"]],
            [("insert", "app_sessions"), ("delete", "app_sessions")],
        )
        payload = db["ops"][0][2]
        with self.client.session_transaction() as sess:
            token = sess["session_token"]
        self.assertEqual(payload["token_hash"], _hash(token))
        self.assertNotEqual(payload["token_hash"], token)
        self.assertEqual(payload["user_id"], "user-1")
        expires = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
        delta = expires - datetime.now(timezone.utc)
        self.assertGreater(delta, timedelta(hours=11, minutes=58))
        self.assertLess(delta, timedelta(hours=12, minutes=2))
        self.assertEqual(db["ors"], [])
        self.assertEqual(len(db["lts"]), 1)
        table_name, column, raw = db["lts"][0]
        self.assertEqual(table_name, "app_sessions")
        self.assertEqual(column, "expires_at")
        cutoff = datetime.fromisoformat(raw)
        self.assertIsNotNone(cutoff.tzinfo)
        age = datetime.now(timezone.utc) - cutoff
        self.assertGreater(age, timedelta(days=7) - timedelta(minutes=2))
        self.assertLess(age, timedelta(days=7) + timedelta(minutes=2))

    def test_login_cleanup_failure_is_logged_and_login_succeeds(self):
        import logging
        db = {
            "ops": [],
            "ors": [],
            "sessions": [],
            "delete_boom": "cleanup failed hard",
        }
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage())
        logger = logging.getLogger("app.routes")
        logger.addHandler(handler)
        try:
            response = self._login(db)
        finally:
            logger.removeHandler(handler)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["redirect"], "/instructor/dashboard")
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("user_id"), "user-1")
        self.assertTrue(any(op[0] == "delete" and op[1] == "app_sessions" for op in db["ops"]))
        self.assertTrue(any(
            "user-1" in message and "cleanup failed hard" in message
            for message in records
        ))

    def test_login_primes_the_cache_so_the_next_request_does_not_read_app_sessions(self):
        from app.app_sessions import clear_app_session_cache
        clear_app_session_cache()
        db = {"ops": [], "ors": [], "sessions": [], "selects": [], "lts": []}
        user = MagicMock()
        user.id = "user-1"
        user.user_metadata = {"full_name": "Ada"}
        auth = MagicMock()
        auth.auth.sign_in_with_password.return_value = MagicMock(user=user)
        admin = _admin(db)
        with patch("app.routes.supabase", auth), \
             patch("app.routes.supabase_admin", admin), \
             patch("app.authentication.supabase_admin", admin):
            response = self.client.post(
                "/api/login",
                json={"email": "ada@school.edu", "password": "secret"},
            )
            db["selects"].clear()
            status = self.client.get("/api/session-status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(status.status_code, 200)
        self.assertNotIn("app_sessions", db["selects"])


class TestAddClassProfile(unittest.TestCase):
    def test_add_class_does_not_write_profiles(self):
        from app import routes
        seen = []

        def table(name):
            seen.append(name)
            chain = MagicMock()
            chain.insert.return_value.execute.return_value.data = [{"id": "new-class"}]
            chain.upsert.return_value.execute.return_value.data = []
            return chain

        app = create_app()
        data = {
            "name": "Test Upload",
            "semester": "Fall",
            "year": "2026",
            "days": "MW",
            "create_class_token": "profile-skip",
            "section_number": "F26",
        }
        with app.test_request_context("/add_class", method="POST", data=data):
            session["user_id"] = "user-1"
            session["role"] = "instructor"
            session["create_class_token"] = "profile-skip"
            with patch("app.routes.supabase_admin") as admin:
                admin.table.side_effect = table
                result = routes.add_class()
        self.assertEqual(result.status_code, 302)
        self.assertNotIn("profiles", seen)
        self.assertFalse(hasattr(routes, "ensure_profile_exists"))


class TestLogoutMarkup(unittest.TestCase):
    def test_logout_controls_post_a_form(self):
        root = os.path.join(os.path.dirname(__file__), "..", "app")
        for name in ("templates/app_base.html", "templates/instructor_select_class.html"):
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                html = handle.read()
            self.assertIn('method="POST"', html)
            self.assertIn("action=\"{{ url_for('main.logout') }}\"", html)
            self.assertNotIn("href=\"{{ url_for('main.logout') }}\"", html)
        script_path = os.path.join(root, "static", "js", "session_timeout.js")
        with open(script_path, encoding="utf-8") as handle:
            script = handle.read()
        self.assertIn('form.action = "/logout"', script)
        self.assertIn('reason.value = "inactivity"', script)
        self.assertIn('meta[name="csrf-token"]', script)
        self.assertIn('csrf.name = "csrf_token"', script)
        self.assertNotIn('location.assign("/logout?reason=inactivity")', script)

    def test_rendered_logout_forms_contain_the_csrf_field(self):
        from flask import render_template, session
        app = create_app()
        with app.test_request_context("/"):
            session["csrf_token"] = "tok-render"
            session["role"] = "instructor"
            base = render_template("app_base.html")
            select = render_template(
                "instructor_select_class.html",
                classes=[],
                create_class_token="create-tok",
                copy_class_token="copy-tok",
                class_days=("MW",),
            )
        for html in (base, select):
            form = html.split("action=\"/logout\"", 1)[1].split("</form>", 1)[0]
            self.assertIn('name="csrf_token"', form)
            self.assertIn('value="tok-render"', form)


class TestSessionCookieBoot(unittest.TestCase):
    def _reload(self, env, drop=()):
        import config
        try:
            with patch.dict(os.environ, env, clear=False):
                for name in drop:
                    os.environ.pop(name, None)
                importlib.reload(config)
                return config.Config
        finally:
            importlib.reload(config)

    def test_invalid_samesite_refuses_to_boot(self):
        with self.assertRaises(RuntimeError):
            self._reload({
                "APP_ENV": "development",
                "SECRET_KEY": "test-secret",
                "SESSION_COOKIE_SAMESITE": "invalid",
                "SESSION_COOKIE_SECURE": "false",
                "RAILWAY_ENVIRONMENT_ID": "",
                "RAILWAY_PROJECT_ID": "",
                "RAILWAY_SERVICE_ID": "",
                "PUBLIC_BASE_URL": "https://clarity.example",
                "CORS_ALLOWED_ORIGINS": "https://clarity.example",
            })

    def test_samesite_none_without_secure_refuses_to_boot(self):
        with self.assertRaises(RuntimeError):
            self._reload({
                "APP_ENV": "development",
                "SECRET_KEY": "test-secret",
                "SESSION_COOKIE_SAMESITE": "None",
                "SESSION_COOKIE_SECURE": "false",
                "RAILWAY_ENVIRONMENT_ID": "",
                "RAILWAY_PROJECT_ID": "",
                "RAILWAY_SERVICE_ID": "",
                "PUBLIC_BASE_URL": "https://clarity.example",
                "CORS_ALLOWED_ORIGINS": "https://clarity.example",
            })

    def test_railway_secure_default_covers_relaxed_app_env(self):
        secure = self._reload({
            "APP_ENV": "staging",
            "SECRET_KEY": "test-secret",
            "SESSION_COOKIE_SAMESITE": "Lax",
            "RAILWAY_ENVIRONMENT_ID": "env-1",
            "RAILWAY_PROJECT_ID": "",
            "RAILWAY_SERVICE_ID": "",
            "PUBLIC_BASE_URL": "https://clarity.example",
            "CORS_ALLOWED_ORIGINS": "https://clarity.example",
        }, drop=("SESSION_COOKIE_SECURE",))
        self.assertTrue(secure.SESSION_COOKIE_SECURE)
        local = self._reload({
            "APP_ENV": "staging",
            "SECRET_KEY": "test-secret",
            "SESSION_COOKIE_SAMESITE": "Lax",
            "RAILWAY_ENVIRONMENT_ID": "",
            "RAILWAY_PROJECT_ID": "",
            "RAILWAY_SERVICE_ID": "",
            "PUBLIC_BASE_URL": "https://clarity.example",
            "CORS_ALLOWED_ORIGINS": "https://clarity.example",
        }, drop=("SESSION_COOKIE_SECURE",))
        self.assertFalse(local.SESSION_COOKIE_SECURE)
