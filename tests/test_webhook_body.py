import unittest

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.schemas import WebhookSignal


class WebhookBodyTests(unittest.TestCase):
    """A TradingView message that isn't a JSON object - e.g. a strategy's
    default alert text - is refused with a 422, not a 500."""

    def setUp(self):
        # Not used as a context manager, so the lifespan's schedulers never start.
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_non_object_bodies_get_422(self):
        bodies = [
            ("ATR Trailing Stop: order buy @ 1 filled on PIEVERSEUSDT.P", "text/plain"),
            ('"hello"', "application/json"),
            ("[1, 2]", "application/json"),
        ]
        for path in ("/webhook", "/webhook/blofin", "/webhook/forex"):
            for body, content_type in bodies:
                with self.subTest(path=path, body=body):
                    r = self.client.post(path, content=body, headers={"content-type": content_type})
                    self.assertEqual(r.status_code, 422, r.text)

    def test_json_text_is_parsed(self):
        # TradingView marks valid JSON as JSON, but a JSON object sent as
        # plain text still reads the same.
        signal = WebhookSignal.model_validate('{"strategy": "S", "symbol": "btcusdt.p", "action": "buy", "market_position": "long"}')
        self.assertEqual((signal.action.value, signal.direction.value, signal.symbol), ("entry", "long", "BTCUSDT.P"))

    def test_plain_text_fails_validation(self):
        with self.assertRaises(ValidationError):
            WebhookSignal.model_validate("not json")


if __name__ == "__main__":
    unittest.main()
