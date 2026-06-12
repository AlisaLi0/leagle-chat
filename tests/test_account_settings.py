import importlib
import os
import sys
import tempfile
import unittest


class AccountSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LEAGLE_DB_PATH"] = os.path.join(self.tmp.name, "test.db")
        os.environ["LEAGLE_SECRET_KEY"] = "test-secret-key"
        # No SMTP configured -> endpoint surfaces dev_code, no real mail sent.
        for var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "SMTP_FROM"):
            os.environ.pop(var, None)
        # Drop cached server.* so modules re-read env at import.
        for mod in [m for m in list(sys.modules) if m.startswith("server")]:
            sys.modules.pop(mod, None)
        import server.db as db
        import server.auth as auth
        import server.email_send as email_send
        import server.app as app
        self.db = importlib.reload(db)
        self.auth = importlib.reload(auth)
        self.email_send = importlib.reload(email_send)
        # app imports db/auth/email_send by reference at module load; reimport so
        # it binds the freshly reloaded modules.
        sys.modules.pop("server.app", None)
        self.app_mod = importlib.import_module("server.app")
        self.db.init_db()
        from fastapi.testclient import TestClient
        self.client = TestClient(self.app_mod.app)

    def tearDown(self):
        self.tmp.cleanup()

    def _signin(self, provider="x", uid="x-1", email="", name="X User"):
        user = self.db.upsert_user(provider, uid, email=email, name=name)
        cookie = self.auth.make_session_cookie(user.id)
        csrf = self.auth.make_csrf_token(cookie)
        self.client.cookies.set(self.auth.COOKIE_NAME, cookie)
        return user, {"X-CSRF-Token": csrf}

    # ── profile ──────────────────────────────────────────────────────────
    def test_profile_update_requires_auth(self):
        r = self.client.post("/api/account/profile", json={"name": "Nope"})
        self.assertEqual(r.status_code, 401)

    def test_profile_update_changes_display_name(self):
        user, headers = self._signin(name="Old Name")
        r = self.client.post("/api/account/profile", json={"name": "  New Name "}, headers=headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["user"]["name"], "New Name")
        self.assertEqual(self.db.get_user(user.id).name, "New Name")

    def test_profile_rejects_empty_name(self):
        _user, headers = self._signin()
        r = self.client.post("/api/account/profile", json={"name": "   "}, headers=headers)
        self.assertEqual(r.status_code, 400)

    def test_profile_requires_csrf(self):
        self._signin()
        r = self.client.post("/api/account/profile", json={"name": "x"})  # no csrf header
        self.assertEqual(r.status_code, 403)

    # ── email verification ───────────────────────────────────────────────
    def test_email_verify_happy_path_sets_email(self):
        user, headers = self._signin(email="")
        start = self.client.post("/api/account/email/start",
                                 json={"email": "me@example.com", "lang": "en"}, headers=headers)
        self.assertEqual(start.status_code, 200)
        body = start.json()
        self.assertFalse(body["sent"])           # SMTP off in tests
        code = body["dev_code"]                   # dev fallback exposes the code
        self.assertEqual(len(code), 6)
        ver = self.client.post("/api/account/email/verify",
                               json={"email": "me@example.com", "code": code}, headers=headers)
        self.assertEqual(ver.status_code, 200)
        self.assertEqual(ver.json()["user"]["email"], "me@example.com")
        self.assertEqual(self.db.get_user(user.id).email, "me@example.com")

    def test_email_verify_bad_code(self):
        _user, headers = self._signin(email="")
        self.client.post("/api/account/email/start", json={"email": "me@example.com"}, headers=headers)
        ver = self.client.post("/api/account/email/verify",
                               json={"email": "me@example.com", "code": "000000"}, headers=headers)
        self.assertEqual(ver.status_code, 400)
        self.assertEqual(ver.json()["error"], "bad_code")

    def test_email_start_rejects_invalid_address(self):
        _user, headers = self._signin()
        r = self.client.post("/api/account/email/start", json={"email": "not-an-email"}, headers=headers)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "invalid_email")

    def test_email_start_conflict_when_used_by_other(self):
        # Another account already owns the target email.
        self.db.upsert_user("google", "g-9", email="taken@example.com", name="G")
        _user, headers = self._signin(provider="x", uid="x-2", email="")
        r = self.client.post("/api/account/email/start",
                             json={"email": "taken@example.com"}, headers=headers)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "email_in_use")

    def test_email_verify_blocks_conflict_at_verify_time(self):
        # Start verification, then a conflicting account appears before verify.
        _user, headers = self._signin(provider="x", uid="x-3", email="")
        start = self.client.post("/api/account/email/start",
                                 json={"email": "race@example.com"}, headers=headers)
        code = start.json()["dev_code"]
        self.db.upsert_user("google", "g-10", email="race@example.com", name="G")
        ver = self.client.post("/api/account/email/verify",
                               json={"email": "race@example.com", "code": code}, headers=headers)
        self.assertEqual(ver.status_code, 409)
        self.assertEqual(ver.json()["error"], "in_use")

    def test_email_start_same_email_rejected(self):
        _user, headers = self._signin(email="mine@example.com")
        r = self.client.post("/api/account/email/start",
                             json={"email": "mine@example.com"}, headers=headers)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "same_email")

    # ── db-level invariants ──────────────────────────────────────────────
    def test_verify_email_code_states(self):
        user = self.db.upsert_user("x", "x-db", email="", name="X")
        self.assertEqual(self.db.verify_email_code(user.id, "a@b.com", "123456"), "no_code")
        self.db.create_email_verification(user.id, "a@b.com", "654321")
        self.assertEqual(self.db.verify_email_code(user.id, "a@b.com", "111111"), "bad_code")
        self.assertEqual(self.db.verify_email_code(user.id, "a@b.com", "654321"), "ok")
        self.assertEqual(self.db.get_user(user.id).email, "a@b.com")

    def test_email_module_disabled_without_config(self):
        self.assertFalse(self.email_send.enabled())
        self.assertEqual(len(self.email_send.gen_code()), 6)


if __name__ == "__main__":
    unittest.main()
