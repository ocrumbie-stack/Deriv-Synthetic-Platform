import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import auth, ctrader, forex, forex_trading
from app.config import settings
from app.ctrader import CTraderError
from app.database import Base
from app.models import ExecutionStatus, ForexBot, ForexSignal, RiskSettings, Signal, Trade
from app.schemas import WebhookSignal

EURUSD = {
    "symbolId": 1, "symbolName": "EURUSD", "digits": 5, "pipPosition": 4,
    "lotSize": 10_000_000, "stepVolume": 100_000, "minVolume": 100_000, "maxVolume": 10_000_000_000,
}
USDJPY = {**EURUSD, "symbolId": 2, "symbolName": "USDJPY", "digits": 3, "pipPosition": 2}
SYMBOLS = {"EURUSD": EURUSD, "USDJPY": USDJPY}


def ny(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)


OPEN = ny(2026, 11, 11, 10)  # Wednesday: London + New York
WEEKEND = ny(2026, 11, 14, 10)  # Saturday


def position(position_id, direction="long", symbol=EURUSD, label=None, comment=None, volume=100_000):
    return {
        "position_id": position_id, "symbol_id": symbol["symbolId"], "direction": direction, "volume": volume,
        "entry_price": 1.1, "stop_loss": None, "take_profit": None, "trailing_stop": False,
        "swap": 0.0, "commission": -0.05, "used_margin": 3.67, "label": label, "comment": comment,
        "opened_at": datetime(2026, 11, 11, 15),
    }


class FakeClient:
    def __init__(self, positions=(), account_id=77):
        self.account_id = account_id
        self.open = {p["position_id"]: p for p in positions}
        self.orders, self.closed = [], []
        self.deals = []
        self.next_id = 500
        self.fail_open = None

    async def resolve_symbol(self, raw):
        if raw not in SYMBOLS:
            raise CTraderError(f"No symbol on this cTrader account matches '{raw}'.")
        return dict(SYMBOLS[raw])

    async def positions(self):
        return list(self.open.values())

    async def open_market(self, symbol, direction, lots, **kwargs):
        if self.fail_open:
            raise CTraderError(self.fail_open)
        self.orders.append({"symbol": symbol["symbolName"], "direction": direction, "lots": lots, **kwargs})
        self.next_id += 1
        volume = ctrader.lots_to_volume(lots, symbol)
        self.open[self.next_id] = position(self.next_id, direction, symbol, kwargs.get("label"), kwargs.get("comment"), volume)
        return {**self.open[self.next_id], "entry_price": 1.10012}

    async def close_position(self, position_id, volume):
        self.open.pop(position_id)
        self.closed.append(position_id)
        return {"position_id": position_id, "net_profit": 1.25}

    async def symbols(self):
        return [{"symbolId": s["symbolId"], "symbolName": s["symbolName"]} for s in SYMBOLS.values()]

    async def symbol_details(self, symbol_id):
        return next(dict(s) for s in SYMBOLS.values() if s["symbolId"] == symbol_id)

    async def closing_deals(self, since, until=None):
        return list(self.deals)

    async def unrealized_pnl(self):
        return {pid: 2.5 for pid in self.open}


class ForexTradingTestCase(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.bot = ForexBot(name="fx", lots=0.01, stop_loss_pips=20, take_profit_pips=40, enabled=True)
        self.db.add_all([RiskSettings(id=1, emergency_stop=False, duplicate_blocking=True), self.bot])
        self.db.commit()
        names = ("webhook_secret", "execution_mode", "emergency_stop")
        self._saved = {n: getattr(settings, n) for n in names}
        settings.webhook_secret, settings.execution_mode, settings.emergency_stop = "s", "demo", False
        self.verify = patch("app.ctrader.verify_account_mode").start()

    def tearDown(self):
        patch.stopall()
        for n, v in self._saved.items():
            setattr(settings, n, v)
        self.db.close()

    def send(self, client, now=OPEN, **fields):
        values = {"secret": "s", "strategy": "fx", "symbol": "EURUSD", "action": "entry", "direction": "long", **fields}
        return asyncio.run(forex_trading.process_signal(self.db, WebhookSignal(**values), client, now))


class EntryTests(ForexTradingTestCase):
    def test_entry_opens_on_the_mode_account_with_pip_stops(self):
        client = FakeClient()
        signal = self.send(client, symbol="FX:EUR/USD")
        self.assertEqual(signal.status, ExecutionStatus.executed, signal.reason)
        order = client.orders[0]
        self.assertEqual((order["symbol"], order["direction"], order["lots"]), ("EURUSD", "long", 0.01))
        self.assertAlmostEqual(order["stop_loss_distance"], 0.002)
        self.assertAlmostEqual(order["take_profit_distance"], 0.004)
        self.assertFalse(order["trailing_stop"])
        self.assertEqual((order["label"], order["comment"]), (forex_trading.LABEL, "fx"))
        self.assertEqual((signal.position_id, signal.account_id, signal.entry_price, signal.lots), (501, 77, 1.10012, 0.01))
        self.verify.assert_called_once_with(77, False, "forex")

    def test_yen_pair_pips_are_hundredths(self):
        client = FakeClient()
        self.send(client, symbol="USDJPY")
        self.assertAlmostEqual(client.orders[0]["stop_loss_distance"], 0.2)

    def test_trailing_stop_needs_a_stop_loss(self):
        self.bot.trailing_stop = True
        self.db.commit()
        client = FakeClient()
        self.send(client)
        self.assertTrue(client.orders[0]["trailing_stop"])
        self.bot.stop_loss_pips = None
        self.db.commit()
        client = FakeClient()
        self.send(client)
        self.assertFalse(client.orders[0]["trailing_stop"])

    def test_drawn_levels_win_over_the_bots_pips(self):
        client = FakeClient()
        self.send(client, price=1.1, stop_loss=1.0985, take_profit=1.103)
        self.assertAlmostEqual(client.orders[0]["stop_loss_distance"], 0.0015)
        self.assertAlmostEqual(client.orders[0]["take_profit_distance"], 0.003)

    def test_entries_are_refused_outside_the_trading_window(self):
        client = FakeClient()
        signal = self.send(client, now=WEEKEND)
        self.assertEqual(signal.status, ExecutionStatus.rejected)
        self.assertIn("weekend", signal.reason)
        self.assertEqual(client.orders, [])

    def test_entry_needs_an_enabled_bot_and_an_allowed_pair(self):
        signal = self.send(FakeClient(), strategy="nobody")
        self.assertIn("No forex bot", signal.reason)
        self.bot.symbols = "GBPUSD"
        self.db.commit()
        self.assertIn("allowed pairs", self.send(FakeClient()).reason)
        self.bot.enabled = False
        self.db.commit()
        self.assertEqual(self.send(FakeClient()).reason, "Forex bot is paused.")

    def test_bad_secret_and_unknown_pair_are_refused(self):
        self.assertEqual(self.send(FakeClient(), secret="nope").reason, "Invalid webhook secret.")
        self.assertEqual(self.send(FakeClient(), symbol="R_75").status, ExecutionStatus.rejected)

    def test_account_of_the_wrong_kind_fails_without_ordering(self):
        self.verify.side_effect = CTraderError("cTrader account 77 is a live account but is picked for demo")
        client = FakeClient()
        signal = self.send(client)
        self.assertEqual(signal.status, ExecutionStatus.failed)
        self.assertEqual(client.orders, [])

    def test_same_direction_entry_is_refused(self):
        client = FakeClient([position(9, "long", label=forex_trading.LABEL, comment="fx")])
        signal = self.send(client)
        self.assertEqual(signal.status, ExecutionStatus.rejected)
        self.assertEqual(client.orders, [])

    def test_opposite_entry_reverses(self):
        client = FakeClient([position(9, "short", label=forex_trading.LABEL, comment="fx")])
        signal = self.send(client)
        self.assertEqual(signal.status, ExecutionStatus.executed)
        self.assertEqual(client.closed, [9])
        self.assertEqual(signal.closed_positions, "9")
        self.assertEqual(signal.reason, "Reversed the open position first.")

    def test_positions_opened_elsewhere_are_left_alone(self):
        # A short opened in the cTrader app on the same pair isn't the strategy's.
        client = FakeClient([position(9, "short")])
        signal = self.send(client)
        self.assertEqual(signal.status, ExecutionStatus.executed)
        self.assertEqual(client.closed, [])

    def test_nothing_is_written_to_the_synthetic_tables(self):
        self.send(FakeClient())
        self.assertEqual(self.db.scalars(select(Signal)).all(), [])
        self.assertEqual(self.db.scalars(select(Trade)).all(), [])


class ExitTests(ForexTradingTestCase):
    def test_exit_closes_the_strategys_position_it_opened(self):
        client = FakeClient()
        entry = self.send(client)
        client.open[entry.position_id]["comment"] = None  # found from the journal alone
        client.open[9] = position(9, "long")  # opened in the cTrader app
        signal = self.send(client, action="exit", direction=None)
        self.assertEqual(signal.status, ExecutionStatus.closed)
        self.assertEqual(client.closed, [entry.position_id])
        self.assertEqual(signal.net_profit, 1.25)

    def test_exit_goes_through_when_paused_and_outside_the_window(self):
        client = FakeClient([position(9, "long", label=forex_trading.LABEL, comment="fx")])
        self.bot.enabled = False
        self.db.commit()
        signal = self.send(client, now=WEEKEND, action="exit", direction=None)
        self.assertEqual(signal.status, ExecutionStatus.closed)

    def test_exit_with_nothing_open_is_refused(self):
        signal = self.send(FakeClient(), action="exit", direction=None)
        self.assertEqual((signal.status, signal.reason), (ExecutionStatus.rejected, "No open position to close."))


class PageTests(ForexTradingTestCase):
    def test_positions_and_history_name_the_strategy_and_reason(self):
        client = FakeClient()
        entry = self.send(client)
        client.open[9] = position(9, "short")
        asyncio.run(forex_trading.close_position(self.db, entry.position_id, client))
        client.deals = [
            {"position_id": entry.position_id, "symbol_id": 1, "direction": "long", "volume": 100_000, "entry_price": 1.1,
             "exit_price": 1.1025, "swap": 0.0, "commission": -0.1, "net_profit": 2.4,
             "opened_at": datetime(2026, 11, 11, 15), "closed_at": datetime(2026, 11, 11, 16)},
            {"position_id": 8, "symbol_id": 2, "direction": "short", "volume": 100_000, "entry_price": 150.0,
             "exit_price": 150.3, "swap": 0.0, "commission": 0.0, "net_profit": -2.0, "closed_at": datetime(2026, 11, 11, 14)},
        ]
        data = asyncio.run(forex_trading.trading(self.db, client))
        self.assertEqual([p["position_id"] for p in data["positions"]], [9])
        self.assertIsNone(data["positions"][0]["strategy"])
        self.assertEqual(data["positions"][0]["lots"], 0.01)
        ours, other = data["history"]
        self.assertEqual((ours["strategy"], ours["reason"], ours["pips"]), ("fx", "Closed from dashboard", 25.0))
        self.assertEqual((other["strategy"], other["pips"]), (None, -30.0))
        self.assertTrue(other["reason"].startswith("cTrader"))
        self.assertTrue(data["signals"][0]["manual"])

    def test_page_without_an_account_says_why(self):
        with patch("app.forex_trading.client_for_mode", side_effect=CTraderError("No cTrader demo account is selected")):
            data = asyncio.run(forex_trading.trading(self.db))
        self.assertFalse(data["connected"])
        self.assertIn("No cTrader demo account", data["error"])


class HelperTests(ForexTradingTestCase):
    def test_trailing_pips_move_onto_the_stop_loss(self):
        self.bot.stop_loss_pips = None
        self.bot.trailing_stop_pips = 15
        self.db.commit()
        forex.move_trailing_pips_to_stop(self.db)
        self.assertEqual((self.bot.trailing_stop, self.bot.stop_loss_pips, self.bot.trailing_stop_pips), (True, 15, None))

    def test_forex_webhook_is_public(self):
        self.assertIn("/webhook/forex", auth.PUBLIC_PATHS)

    def test_closing_deal_reports_the_positions_side_and_times(self):
        deal = {
            "positionId": 5, "symbolId": 1, "tradeSide": ctrader.SIDE_SELL, "executionPrice": 1.2,
            "executionTimestamp": 1_762_876_800_000,
            "closePositionDetail": {"entryPrice": 1.1, "grossProfit": 100, "moneyDigits": 2, "closedVolume": 100_000},
        }
        out = ctrader.close_out(deal)
        self.assertEqual((out["direction"], out["volume"], out["symbol_id"]), ("long", 100_000, 1))
        self.assertIsNotNone(out["closed_at"])


if __name__ == "__main__":
    unittest.main()
