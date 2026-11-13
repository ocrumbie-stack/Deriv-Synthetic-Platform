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
        crypto._cache.clear()
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

    def test_bot_tp_sl_are_a_percent_of_margin(self):
        self.bot.tp_pct, self.bot.sl_pct = 50, 25
        self.db.commit()
        client = self._client()
        client.instrument.return_value = {**BTC, "tickSize": "0.1"}
        payload = WebhookSignal(secret="s", strategy="btc", symbol="BTCUSDT", action="entry", direction="long")
        signal = asyncio.run(crypto.process_signal(self.db, payload, client))
        kwargs = client.open_market.call_args.kwargs
        # 50% of the margin at 10x is a 5% move from $50,000; 25% is 2.5%.
        self.assertEqual((kwargs["take_profit"], kwargs["stop_loss"]), ("52500.0", "48750.0"))
        self.assertEqual((signal.take_profit, signal.stop_loss), (52500.0, 48750.0))

    def test_short_stops_mirror_and_alert_prices_win(self):
        self.bot.tp_pct, self.bot.sl_pct = 50, 25
        self.db.commit()
        signal, client = self._send(direction="short", stop_loss=51000)
        kwargs = client.open_market.call_args.kwargs
        self.assertEqual((kwargs["take_profit"], kwargs["stop_loss"]), ("47500.000", 51000))
        self.assertEqual((signal.take_profit, signal.stop_loss), (47500.0, 51000.0))

    def test_small_prices_round_to_the_tick_as_plain_decimals(self):
        bot = CryptoBot(name="pepe", margin=10, leverage=5, tp_pct=10, sl_pct=None)
        take_profit, stop_loss = crypto.stop_prices(bot, crypto.Direction.long, 0.00001234, "0.00000001")
        self.assertEqual((take_profit, stop_loss), ("0.00001259", None))

    def test_overview_lists_bot_pairs_and_position_owner(self):
        self._send()
        self._send(symbol="ETHUSDT", direction="short")
        client = self._client([
            {"symbol": "BTC-USDT", "side": "LONG"}, {"symbol": "ETH-USDT", "side": "LONG"}, {"symbol": "SOL-USDT", "side": "LONG"},
        ])
        client.balance.return_value = {"equity": 100.0, "available": 90.0, "unrealized_pnl": 0.0}
        data = asyncio.run(crypto.overview(self.db, client))
        pairs = {p["symbol"]: p for p in data["bots"][0]["pairs"]}
        self.assertEqual({s: (p["entries"], p["direction"]) for s, p in pairs.items()},
                         {"BTC-USDT": (1, "long"), "ETH-USDT": (1, "short")})
        # The ETH long isn't the bot's short, and nothing opened SOL here.
        self.assertEqual([p["strategy"] for p in data["positions"]], ["btc", None, None])

    def test_api_key_failure_only_hides_the_badges(self):
        client = self._client()
        client.balance.return_value = {"equity": 100.0, "available": 90.0, "unrealized_pnl": 0.0}
        client.api_key.side_effect = BlofinError("BloFin error 152401: access key does not exist")
        data = asyncio.run(crypto.overview(self.db, client))
        self.assertIsNone(data["api_key"])
        self.assertIsNone(data["error"])


class CryptoMarketTests(unittest.TestCase):
    def setUp(self):
        crypto._cache.clear()
        self._mode = settings.execution_mode
        settings.execution_mode = "demo"

    def tearDown(self):
        crypto._cache.clear()
        settings.execution_mode = self._mode

    def _client(self):
        client = AsyncMock()
        client.tickers.return_value = {
            "BTC-USDT": {"last": 110.0, "open_24h": 100.0, "high_24h": 120.0, "low_24h": 90.0, "volume_24h": 5e9},
            "ETH-USDT": {"last": 50.0, "open_24h": None, "high_24h": None, "low_24h": None, "volume_24h": None},
        }
        client.funding_rates.return_value = {"BTC-USDT": {"rate": 0.0001, "next_time": 1, "interval_hours": 8.0}}
        client.closes.return_value = [100.0, 105.0, 110.0]
        return client

    def test_pairs_come_back_in_order_with_change_and_funding(self):
        data = asyncio.run(crypto.market("ethusdt.p, BTCUSDT, BTC-USDT, NOPE-USDT, BTCUSD", self._client()))
        self.assertEqual([p["symbol"] for p in data["pairs"]], ["ETH-USDT", "BTC-USDT"])
        btc = data["pairs"][1]
        self.assertAlmostEqual(btc["change_24h"], 0.1)
        self.assertEqual(btc["funding"]["rate"], 0.0001)
        self.assertEqual(btc["sparkline"], [100.0, 105.0, 110.0])
        self.assertIsNone(data["pairs"][0]["change_24h"])
        self.assertIsNone(data["pairs"][0]["funding"])

    def test_market_data_is_cached(self):
        client = self._client()
        asyncio.run(crypto.market("BTCUSDT", client))
        asyncio.run(crypto.market("BTCUSDT", client))
        self.assertEqual(client.tickers.await_count, 1)
        self.assertEqual(client.closes.await_count, 1)

    def test_failed_sparkline_keeps_the_price(self):
        client = self._client()
        client.closes.side_effect = BlofinError("BloFin HTTP 429: too many requests")
        data = asyncio.run(crypto.market("BTCUSDT", client))
        self.assertEqual(data["pairs"][0]["last"], 110.0)
        self.assertEqual(data["pairs"][0]["sparkline"], [])

    def test_ticker_failure_reports_an_error(self):
        client = self._client()
        client.tickers.side_effect = BlofinError("BloFin HTTP 503: down")
        data = asyncio.run(crypto.market("BTCUSDT", client))
        self.assertEqual((data["pairs"], data["error"]), ([], "BloFin HTTP 503: down"))

    def test_no_pairs_makes_no_calls(self):
        client = self._client()
        self.assertEqual(asyncio.run(crypto.market("", client))["pairs"], [])
        client.tickers.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
