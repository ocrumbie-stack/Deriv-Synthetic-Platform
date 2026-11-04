import asyncio
import base64
import hashlib
import hmac
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crypto
from app.blofin import BlofinError, _position, contract_symbol, contracts_for, sign
from app.config import settings
from app.database import Base
from app.models import CryptoBot, CryptoSignal, RiskSettings
from app.schemas import WebhookSignal

BTC = {"instId": "BTC-USDT", "contractValue": "0.001", "lotSize": "0.1", "minSize": "0.1", "state": "live"}


def position(side="long", size="1", inst="BTC-USDT"):
    return _position({"instId": inst, "positionSide": "net", "positions": size if side == "long" else f"-{size}", "marginMode": "isolated"})


class BlofinHelperTests(unittest.TestCase):
    def test_contract_symbol(self):
        self.assertEqual(contract_symbol("BTCUSDT"), "BTC-USDT")
        self.assertEqual(contract_symbol("btcusdt.p"), "BTC-USDT")
        self.assertEqual(contract_symbol("ETHUSDT.PERP"), "ETH-USDT")
        self.assertEqual(contract_symbol("SOL-USDT"), "SOL-USDT")
        # Saved while the page was on Toobit.
        self.assertEqual(contract_symbol("SOL-SWAP-USDT"), "SOL-USDT")
        with self.assertRaises(BlofinError):
            contract_symbol("BTCUSD")

    def test_sign_is_base64_of_hex_digest(self):
        prehash = "/api/v1/trade/orderPOST1597026383085abc{\"instId\":\"BTC-USDT\"}"
        hexdigest = hmac.new(b"secret", prehash.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(sign(prehash, "secret"), base64.b64encode(hexdigest.encode()).decode())

    def test_contracts_round_down_to_lot(self):
        # $125 at $50,000 is 0.0025 BTC = 2.5 contracts of 0.001.
        self.assertEqual(contracts_for(125, 50000, "0.001", "0.1"), Decimal("2.5"))
        self.assertEqual(contracts_for(129, 50000, "0.001", "1"), Decimal("2"))

    def test_net_position_side_comes_from_sign(self):
        self.assertEqual(position("long")["side"], "LONG")
        short = position("short", "3")
        self.assertEqual((short["side"], short["contracts"], short["position_side"]), ("SHORT", 3.0, "net"))


class CryptoSignalTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.risk = RiskSettings(id=1, emergency_stop=False, duplicate_blocking=True)
        self.bot = CryptoBot(name="btc", margin=12.5, leverage=10, enabled=True)
        self.db.add_all([self.risk, self.bot])
        self.db.commit()
        names = ("webhook_secret", "execution_mode", "emergency_stop", "blofin_demo_api_key", "blofin_demo_api_secret", "blofin_demo_api_passphrase", "blofin_api_key")
        self._saved = {n: getattr(settings, n) for n in names}
        for n, v in zip(names, ("s", "demo", False, "k", "x", "p", "")):
            setattr(settings, n, v)

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(settings, n, v)
        self.db.close()

    def _client(self, positions=()):
        client = AsyncMock()
        client.positions.return_value = list(positions)
        client.instrument.return_value = dict(BTC)
        client.last_price.return_value = 50000.0
        client.hedge_mode.return_value = False
        client.open_market.return_value = {"orderId": "9", "code": "0"}
        return client

    def _send(self, positions=(), secret="s", **fields):
        client = self._client(positions)
        values = {"secret": secret, "strategy": "btc", "symbol": "BTCUSDT.P", "action": "entry", "direction": "long", **fields}
        signal = asyncio.run(crypto.process_signal(self.db, WebhookSignal(**values), client))
        return signal, client

    def test_entry_uses_bot_size_not_alert_size(self):
        signal, client = self._send(take_profit=70000, stop_loss=60000, size=50, leverage=20)
        self.assertEqual(signal.status.value, "executed")
        self.assertEqual(signal.symbol, "BTC-USDT")
        self.assertEqual(signal.order_id, "9")
        self.assertEqual(signal.execution_mode, "demo")
        client.set_leverage.assert_awaited_once_with("BTC-USDT", 10, None)
        args, kwargs = client.open_market.call_args
        # $12.50 margin at 10x is $125: 2.5 contracts of 0.001 BTC at $50,000.
        self.assertEqual(args[:3], ("BTC-USDT", "long", Decimal("2.5")))
        self.assertEqual(kwargs["position_side"], "net")
        self.assertEqual((kwargs["take_profit"], kwargs["stop_loss"]), (70000, 60000))
        self.assertEqual(signal.margin, 12.5)

    def test_hedge_mode_sends_the_direction_as_position_side(self):
        client = self._client()
        client.hedge_mode.return_value = True
        payload = WebhookSignal(secret="s", strategy="btc", symbol="BTCUSDT", action="entry", direction="short")
        asyncio.run(crypto.process_signal(self.db, payload, client))
        client.set_leverage.assert_awaited_once_with("BTC-USDT", 10, "short")
        self.assertEqual(client.open_market.call_args.kwargs["position_side"], "short")

    def test_position_under_the_minimum_is_refused(self):
        self.bot.margin, self.bot.leverage = 0.5, 1
        self.db.commit()
        signal, client = self._send()
        self.assertEqual(signal.status.value, "rejected")
        self.assertIn("smallest", signal.reason)
        client.open_market.assert_not_awaited()

    def test_entry_without_bot_is_refused(self):
        signal, client = self._send(strategy="unknown")
        self.assertEqual(signal.status.value, "rejected")
        client.positions.assert_not_awaited()

    def test_paused_bot_blocks_entries_but_not_exits(self):
        self.bot.enabled = False
        self.db.commit()
        signal, client = self._send()
        self.assertEqual(signal.status.value, "rejected")
        client.open_market.assert_not_awaited()
        signal, client = self._send(positions=[position("long")], action="exit", direction=None)
        self.assertEqual(signal.status.value, "closed")

    def test_allowed_pairs(self):
        self.bot.symbols = crypto.normalize_symbols("ethusdt, ETH-USDT")
        self.db.commit()
        self.assertEqual(self.bot.symbols, "ETH-USDT")
        signal, _ = self._send()
        self.assertEqual(signal.status.value, "rejected")
        signal, _ = self._send(symbol="ETHUSDT.P")
        self.assertEqual(signal.status.value, "executed")

    def test_allowed_pairs_saved_in_toobit_form_still_match(self):
        self.bot.symbols = "BTC-SWAP-USDT"
        self.db.commit()
        signal, _ = self._send()
        self.assertEqual(signal.status.value, "executed")

    def test_entry_reverses_opposite_position(self):
        short = position("short")
        signal, client = self._send(positions=[short])
        self.assertEqual(signal.status.value, "executed")
        self.assertEqual(client.close_position.call_args.args[0], short)
        client.open_market.assert_awaited_once()

    def test_same_direction_entry_is_refused(self):
        signal, client = self._send(positions=[position("long")])
        self.assertEqual(signal.status.value, "rejected")
        client.open_market.assert_not_awaited()
        client.close_position.assert_not_awaited()

    def test_bad_secret_never_calls_blofin(self):
        signal, client = self._send(secret="wrong")
        self.assertEqual(signal.status.value, "rejected")
        client.positions.assert_not_awaited()

    def test_emergency_stop_blocks_entries_but_not_exits(self):
        self.risk.emergency_stop = True
        self.db.commit()
        signal, client = self._send()
        self.assertEqual(signal.status.value, "rejected")
        client.open_market.assert_not_awaited()
        signal, client = self._send(positions=[position("long")], action="exit", direction=None)
        self.assertEqual(signal.status.value, "closed")
        client.close_position.assert_awaited_once()

    def test_exit_with_nothing_open_is_rejected(self):
        signal, _ = self._send(action="exit", direction=None)
        self.assertEqual(signal.status.value, "rejected")

    def test_mode_without_keys_is_rejected(self):
        # Demo keys are set, live keys aren't.
        settings.execution_mode = "live"
        signal, client = self._send()
        self.assertEqual(signal.status.value, "rejected")
        self.assertIn("BLOFIN_API_KEY", signal.reason)
        self.assertEqual(signal.execution_mode, "live")
        client.positions.assert_not_awaited()

    def test_blofin_error_marks_failed(self):
        client = self._client()
        client.open_market.side_effect = BlofinError("BloFin error 102015: insufficient balance")
        payload = WebhookSignal(secret="s", strategy="btc", symbol="BTCUSDT", action="entry", direction="short")
        signal = asyncio.run(crypto.process_signal(self.db, payload, client))
        self.assertEqual(signal.status.value, "failed")
        self.assertIn("102015", signal.reason)

    def test_overview_only_shows_the_current_mode(self):
        self.db.add_all([
            CryptoSignal(strategy_name="btc", symbol="BTC-USDT", action="entry", execution_mode="demo"),
            CryptoSignal(strategy_name="btc", symbol="BTC-USDT", action="entry", execution_mode="live"),
        ])
        self.db.commit()
        client = self._client()
        client.balance.return_value = {"equity": 100.0, "available": 90.0, "unrealized_pnl": 0.0}
        data = asyncio.run(crypto.overview(self.db, client))
        self.assertEqual(data["mode"], "demo")
        self.assertEqual(len(data["signals"]), 1)


if __name__ == "__main__":
    unittest.main()
