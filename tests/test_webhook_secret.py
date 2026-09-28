import time
import unittest

from fastapi.testclient import TestClient

from app import auth
from app.config import settings
from app.main import app


class WebhookSecretEndpointTests(unittest.TestCase):
    """Copy buttons get the real secret only from behind Google sign-in."""

    def setUp(self):
        names = ("webhook_secret", "google_client_id", "google_client_secret", "allowed_emails", "session_secret")
        self._saved = {n: getattr(settings, n) for n in names}
        settings.webhook_secret = "real-secret"
        settings.session_secret = "test-session"
        # Not used as a context manager, so the lifespan's background
        # schedulers never start.
        self.client = TestClient(app)

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(settings, name, value)

    def _enable_auth(self):
        settings.google_client_id = "id"
        settings.google_client_secret = "secret"
        settings.allowed_emails = "me@example.com"

    def test_signed_in_gets_real_secret(self):
        self._enable_auth()
        token = auth._sign({"email": "me@example.com", "exp": time.time() + 60})
        self.client.cookies.set(auth.SESSION_COOKIE, token)
        r = self.client.get("/api/webhook-secret")
        self.assertEqual(r.json(), {"secret": "real-secret"})
        self.assertEqual(r.headers["cache-control"], "no-store")

    def test_signed_out_is_refused(self):
        self._enable_auth()
        r = self.client.get("/api/webhook-secret")
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("real-secret", r.text)

    def test_never_served_without_sign_in_configured(self):
        settings.google_client_id = settings.google_client_secret = settings.allowed_emails = ""
        r = self.client.get("/api/webhook-secret")
        self.assertEqual(r.json(), {"secret": None})


if __name__ == "__main__":
    unittest.main()
