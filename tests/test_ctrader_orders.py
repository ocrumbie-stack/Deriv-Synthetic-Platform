import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import ctrader, services
from app.config import settings
from app.ctrader import CTraderError
from app.database import Base
from app.models import PositionStatus, RiskSettings, Signal, SignalBot, Trade
from app.schemas import WebhookSignal

# Volatility 75 on Deriv's cTrader: lotSize/step/min in protocol cents.
SYMBOL = {"symbolId": 7, "symbolName": "Volatility 75 Index", "lotSize": 100, "stepVolume": 1, "minVolume": 1, "maxVolume": 100000, "digits": 2}


def fake_client(account_id=501):
    client = MagicMock()
    client.account_id = account_id
    client.resolve_symbol = AsyncMock(return_value=SYMBOL)
    client.open_market = AsyncMock(return_value={"position_id": 9001, "volume": 50, "entry_price": 400000.5, "commission": 0.0})
    client.positions = AsyncMock(return_value=[{"position_id": 9001, "volume": 50}])
    client.close_position = AsyncMock(return_value={
        "position_id": 9001, "exit_price": 400100.0, "entry_price": 400000.5,
        "gross_profit": 49.75, "swap": 0.0, "commission": -0.5, "net_profit": 49.25,
    })
    client.closing_deals = AsyncMock(return_value=[])
    return client


class CTraderOrderTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.risk = RiskSettings(id=1, emergency_stop=False, duplicate_blocking=True, execution_mode_override="demo")
        self.bot = SignalBot(name="v75", size=1, leverage=0, enabled=True, hedge_mode=False, account="ctrader", lots=0.5, sl_usd=25.0, tp_usd=50.0)
        self.db.add_all([self.risk, self.bot])
        self.db.commit()
        self._secret, self._mode = settings.webhook_secret, settings.execution_mode
        settings.webhook_secret = "s"
        services._ctrader_positions.clear()

    def tearDown(self):
        settings.webhook_secret, settings.execution_mode = self._secret, self._mode
        self.db.close()

    def _send(self, client, action="entry", direction="long", **extra):
        payload = WebhookSignal(secret="s", strategy="v75", symbol="VOLATILITY_75_INDEX", action=action, direction=direction, **extra)
        deriv = AsyncMock()
        with patch.object(services, "ctrader_client", return_value=client) as factory, \
             patch.object(ctrader, "verify_account_mode") as verify, \
             patch.object(services, "DerivClient", return_value=deriv):
            result = asyncio.run(services.process_webhook_signal(self.db, payload))
        return result, factory, verify, deriv

    def test_entry_opens_on_ctrader_with_dollar_stops(self):
        client = fake_client()
        result, factory, verify, deriv = self._send(client)
        self.assertEqual(result.signal.status.value, "executed", result.signal.rejection_reason)
        factory.assert_called_with(False)
        verify.assert_called_once_with(501, False)
        deriv.place_order.assert_not_called()
        args, kwargs = client.open_market.call_args
        self.assertEqual(args[1:], ("long", 0.5))
        # 0.5 lots = 50 cents = 0.5 units: $25 is a 50.0 move, $50 a 100.0 move.
        self.assertEqual(kwargs["stop_loss_distance"], 50.0)
        self.assertEqual(kwargs["take_profit_distance"], 100.0)
        self.assertFalse(kwargs["trailing_stop"])
        trade = result.trade
        self.assertEqual((trade.account, trade.exchange_order_id, trade.ctrader_account_id), ("ctrader", "9001", 501))
        self.assertEqual((trade.size, trade.entry_price, trade.execution_mode), (0.5, 400000.5, "demo"))

    def test_live_mode_uses_live_client(self):
        self.risk.execution_mode_override = "live"
        self.db.commit()
        result, factory, verify, _ = self._send(fake_client())
        self.assertEqual(result.signal.status.value, "executed", result.signal.rejection_reason)
        factory.assert_called_with(True)
        verify.assert_called_once_with(501, True)
        self.assertEqual(result.trade.execution_mode, "live")

    def test_drawn_levels_beat_bot_dollars(self):
        client = fake_client()
        self._send(client, price=400000.0, stop_loss=399900.0, take_profit=400300.0)
        kwargs = client.open_market.call_args.kwargs
        self.assertEqual((kwargs["stop_loss_distance"], kwargs["take_profit_distance"]), (100.0, 300.0))

    def test_trailing_stop_sent_with_stop_loss(self):
        self.bot.trailing_stop = True
        self.db.commit()
        client = fake_client()
        self._send(client)
        self.assertTrue(client.open_market.call_args.kwargs["trailing_stop"])

    def test_account_mismatch_fails_entry(self):
        client = fake_client()
        payload = WebhookSignal(secret="s", strategy="v75", symbol="VOLATILITY_75_INDEX", action="entry", direction="long")
        with patch.object(services, "ctrader_client", return_value=client), \
             patch.object(ctrader, "verify_account_mode", side_effect=CTraderError("is a live account but is picked for demo")):
            result = asyncio.run(services.process_webhook_signal(self.db, payload))
        self.assertEqual(result.signal.status.value, "failed")
        self.assertIn("picked for demo", result.signal.rejection_reason)
        client.open_market.assert_not_called()
        self.assertEqual(result.trade.status, PositionStatus.closed)

    def test_no_account_picked_fails_entry(self):
        client = fake_client()
        type(client).account_id = property(lambda self: (_ for _ in ()).throw(CTraderError("No cTrader demo account is selected")))
        result, *_ = self._send(client)
        self.assertEqual(result.signal.status.value, "failed")
        self.assertIn("No cTrader demo account", result.signal.rejection_reason)

    def _open_trade(self, mode="demo", account_id=501):
        strategy = services.get_or_create_strategy(self.db, "v75")
        trade = Trade(
            strategy_id=strategy.id, strategy_name="v75", symbol="VOLATILITY_75_INDEX", direction="long",
            entry_price=400000.5, size=0.5, leverage=1, execution_mode=mode, account="ctrader",
            exchange_order_id="9001", ctrader_account_id=account_id,
        )
        self.db.add(trade)
        self.db.commit()
        return trade

    def test_exit_closes_on_the_account_it_opened_on(self):
        trade = self._open_trade(account_id=777)
        client = fake_client(account_id=777)
        result, factory, _, deriv = self._send(client, action="exit", direction=None)
        self.assertEqual(result.signal.status.value, "closed", result.signal.rejection_reason)
        factory.assert_called_with(False, 777)
        client.close_position.assert_called_once_with(9001, 50)
        deriv.close_order.assert_not_called()
        self.db.refresh(trade)
        self.assertEqual(trade.status, PositionStatus.closed)
        self.assertEqual((trade.net_result, trade.profit_loss, trade.fees, trade.exit_price), (49.25, 49.75, 0.5, 400100.0))
        self.assertEqual(trade.close_reason, "strategy_exit")

    def test_exit_after_stop_hit_records_ctrader_close(self):
        trade = self._open_trade()
        client = fake_client()
        client.positions.return_value = []
        client.closing_deals.return_value = [{
            "position_id": 9001, "exit_price": 399950.5, "entry_price": 400000.5,
            "gross_profit": -25.0, "swap": 0.0, "commission": -0.5, "net_profit": -25.5,
        }]
        result, *_ = self._send(client, action="exit", direction=None)
        self.assertNotEqual(result.signal.status.value, "failed", result.signal.rejection_reason)
        client.close_position.assert_not_called()
        self.db.refresh(trade)
        self.assertEqual((trade.status, trade.close_reason, trade.net_result), (PositionStatus.closed, "ctrader_close", -25.5))
        sources = [s.source for s in self.db.scalars(select(Signal))]
        self.assertIn("ctrader_close", sources)

    def test_reversal_closes_then_opens(self):
        self._open_trade()
        client = fake_client()
        client.open_market.return_value = {"position_id": 9002, "volume": 50, "entry_price": 400100.0, "commission": 0.0}
        result, *_ = self._send(client, direction="short")
        self.assertEqual(result.signal.status.value, "executed", result.signal.rejection_reason)
        client.close_position.assert_called_once()
        self.assertEqual(client.open_market.call_args.args[1], "short")
        self.assertEqual(result.trade.exchange_order_id, "9002")

    def test_failed_reversal_close_stops_entry(self):
        self._open_trade()
        client = fake_client()
        client.close_position.side_effect = CTraderError("MARKET_CLOSED")
        result, *_ = self._send(client, direction="short")
        self.assertEqual(result.signal.status.value, "failed")
        client.open_market.assert_not_called()

    def test_reconcile_keeps_open_position(self):
        trade = self._open_trade()
        with patch.object(services, "ctrader_client", return_value=fake_client()):
            self.assertFalse(asyncio.run(services.reconcile_open_trade(self.db, trade)))
        self.assertEqual(trade.status, PositionStatus.open)

    def test_multipliers_bot_unchanged(self):
        self.bot.account = "options"
        self.bot.size = 5.0
        self.db.commit()
        deriv = AsyncMock()
        deriv.resolve_symbol.return_value = "R_75"
        deriv.place_order.return_value = {"order_id": "123"}
        client = fake_client()
        payload = WebhookSignal(secret="s", strategy="v75", symbol="VOLATILITY_75_INDEX", action="entry", direction="long")
        with patch.object(services, "DerivClient", return_value=deriv), \
             patch.object(services, "ctrader_client", return_value=client):
            result = asyncio.run(services.process_webhook_signal(self.db, payload))
        self.assertEqual(result.signal.status.value, "executed", result.signal.rejection_reason)
        deriv.place_order.assert_called_once()
        client.open_market.assert_not_called()
        self.assertEqual((result.trade.account, result.trade.size), ("options", 5.0))

    def test_cfd_lots_never_count_as_stake_exposure(self):
        self._open_trade()
        self.assertEqual(services.account_exposure(self.db), 0.0)


class DollarDistanceTests(unittest.TestCase):
    def test_rounds_to_symbol_digits(self):
        self.assertEqual(ctrader.dollars_to_distance(10, 300, {"digits": 2}), 3.33)

    def test_too_small_refused(self):
        with self.assertRaises(CTraderError):
            ctrader.dollars_to_distance(0.001, 100000, {"digits": 2})


if __name__ == "__main__":
    unittest.main()
