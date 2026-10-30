import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import services
from app.config import settings
from app.database import Base
from app.deriv import DerivClient, chart_tpsl_amounts
from app.models import ExecutionStatus, RiskSettings, SignalBot
from app.schemas import WebhookSignal


def _signal(**overrides):
    values = dict(
        secret="s", strategy="Chart Trades", symbol="R_75", action="entry",
        direction="long", price=1000.0, size=10.0, take_profit=1010.0, stop_loss=995.0,
    )
    values.update(overrides)
    return WebhookSignal(**values)


class ChartTpSlAmountTests(unittest.TestCase):
    """A drawn trade's TP/SL price levels become Deriv's dollar amounts:
    stake * multiplier * move / entry, net of commission."""

    def test_long_levels_convert_to_dollar_amounts(self):
        # 1% up at 50x on $10 = $5 profit; 0.5% down = $2.50 loss.
        self.assertEqual(chart_tpsl_amounts(_signal(), 1000.0, 50), (5.0, 2.5))

    def test_short_levels_convert_to_dollar_amounts(self):
        signal = _signal(direction="short", take_profit=990.0, stop_loss=1005.0)
        self.assertEqual(chart_tpsl_amounts(signal, 1000.0, 50), (5.0, 2.5))

    def test_commission_comes_out_of_tp_and_adds_to_sl(self):
        self.assertEqual(chart_tpsl_amounts(_signal(), 1000.0, 50, commission=0.5), (4.5, 3.0))

    def test_stop_at_full_stake_or_more_is_left_off(self):
        # 2% down at 50x is the whole $10 stake - Deriv stops it out anyway.
        self.assertEqual(chart_tpsl_amounts(_signal(stop_loss=980.0), 1000.0, 50), (5.0, None))

    def test_unsent_side_and_missing_entry_give_none(self):
        self.assertEqual(chart_tpsl_amounts(_signal(take_profit=None), 1000.0, 50), (None, 2.5))
        self.assertEqual(chart_tpsl_amounts(_signal(), None, 50), (None, None))


class ChartTpSlFillCorrectionTests(unittest.TestCase):
    """Once Deriv fills, the drawn levels are re-measured from the actual
    entry and commission, keeping any side the drawing didn't set."""

    def test_correction_uses_actual_fill_and_keeps_bot_side(self):
        client = DerivClient.__new__(DerivClient)
        client.update_contract_tpsl = AsyncMock()
        signal = _signal(stop_loss=None)
        contract = {"entry_spot": "1002", "commission": "0.1"}
        asyncio.run(client._correct_chart_tpsl("9", signal, contract, 50, 5.0, 1.5))
        # (1010 - 1002) / 1002 * 500 - 0.1 = 3.89; bot's 1.5 SL kept.
        client.update_contract_tpsl.assert_awaited_once_with("9", 3.89, 1.5)

    def test_no_update_when_amounts_already_match(self):
        client = DerivClient.__new__(DerivClient)
        client.update_contract_tpsl = AsyncMock()
        asyncio.run(client._correct_chart_tpsl("9", _signal(), {"entry_spot": 1000.0}, 50, 5.0, 2.5))
        client.update_contract_tpsl.assert_not_awaited()


class ContractIdIsSentAsNumberTests(unittest.TestCase):
    """Deriv refuses a text contract_id ("must be an integer"), which blanked
    live P/L and failed every dashboard close."""

    def test_lookup_and_sell_send_integer_ids(self):
        client = DerivClient.__new__(DerivClient)
        calls = []

        async def rpc(method, params=None):
            calls.append((method, params))
            if method == "proposal_open_contract":
                return {"proposal_open_contract": {"is_sold": 0}}
            return {"sell": {"transaction_id": 1}}

        client._rpc = rpc
        asyncio.run(client.get_contract_status("18877955999", use_cache=False))
        asyncio.run(client.close_order("18877955999"))
        self.assertEqual(calls[0], ("proposal_open_contract", {"contract_id": 18877955999}))
        self.assertIn(("sell", {"sell": 18877955999, "price": 0}), calls)


class ChartTpSlValidationTests(unittest.TestCase):
    def setUp(self):
        # One shared connection, usable from TestClient's request thread too.
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.db.add_all([
            RiskSettings(id=1, emergency_stop=False, duplicate_blocking=True, execution_mode_override="demo"),
            SignalBot(name="Chart Trades", size=10.0, leverage=0, enabled=True, hedge_mode=False),
        ])
        self.db.commit()
        self._secret, self._mode = settings.webhook_secret, settings.execution_mode
        settings.webhook_secret = "s"

    def tearDown(self):
        settings.webhook_secret, settings.execution_mode = self._secret, self._mode
        self.db.close()

    def _process(self, payload):
        client = AsyncMock()
        client.resolve_symbol.return_value = "R_75"
        client.place_order.return_value = {"order_id": "1"}
        with patch.object(services, "DerivClient", return_value=client), \
                patch.object(services, "get_symbol_leverage", AsyncMock(return_value=50)):
            return asyncio.run(services.process_webhook_signal(self.db, payload)), client

    def test_wrong_side_take_profit_is_rejected(self):
        result, client = self._process(_signal(take_profit=990.0))
        self.assertEqual(result.signal.status, ExecutionStatus.rejected)
        self.assertIn("Take profit", result.signal.rejection_reason)
        client.place_order.assert_not_called()

    def test_wrong_side_stop_loss_is_rejected(self):
        result, _ = self._process(_signal(direction="short", take_profit=990.0, stop_loss=995.0))
        self.assertEqual(result.signal.status, ExecutionStatus.rejected)
        self.assertIn("Stop loss", result.signal.rejection_reason)

    def test_valid_drawn_trade_executes(self):
        result, client = self._process(_signal())
        self.assertEqual(result.signal.status, ExecutionStatus.executed)
        sent = client.place_order.call_args.args[0]
        self.assertEqual((sent.take_profit, sent.stop_loss), (1010.0, 995.0))


class ChartTradePreviewTests(ChartTpSlValidationTests):
    """The panel's before-Send preview: the bot's stake, the multiplier
    Deriv will run and the dollar TP/SL, with a warning when the stop can't
    be set because Deriv would stop the trade out first."""

    def _preview(self, payload, multiplier):
        client = AsyncMock()
        client.resolve_symbol.return_value = "R_75"
        client.order_multiplier.return_value = multiplier
        with patch.object(services, "DerivClient", return_value=client), \
                patch.object(services, "get_symbol_leverage", AsyncMock(return_value=multiplier)):
            return asyncio.run(services.preview_chart_trade(self.db, payload))

    def test_shows_stake_multiplier_and_amounts(self):
        preview = self._preview(_signal(), 50)
        self.assertEqual((preview["stake"], preview["multiplier"]), (10.0, 50))
        self.assertEqual((preview["take_profit"], preview["stop_loss"]), (5.0, 2.5))
        self.assertEqual(preview["warnings"], [])

    def test_warns_when_stop_is_past_stop_out(self):
        # 750x on $10: stopped out 1000/750 = 1.33 points down, before 995.
        preview = self._preview(_signal(), 750)
        self.assertIsNone(preview["stop_loss"])
        self.assertAlmostEqual(preview["stop_out_price"], 998.6667, places=3)
        self.assertIn("stop-out", preview["warnings"][0])

    def test_blank_side_uses_bot_percent(self):
        self.db.query(SignalBot).update({"default_pair_sl_pct": 20.0})
        preview = self._preview(_signal(stop_loss=None), 50)
        self.assertEqual(preview["stop_loss"], 2.0)

    def test_wrong_secret_and_missing_bot_are_errors(self):
        self.assertIn("secret", self._preview(_signal(secret="x"), 50)["error"])
        self.assertIn("No signal bot", self._preview(_signal(strategy="Nope"), 50)["error"])


class SignalStatusEndpointTests(ChartTpSlValidationTests):
    """The extension's "was it placed?" check after Send."""

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from app.database import get_db
        from app.main import app
        app.dependency_overrides[get_db] = lambda: self.db
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app)

    def _status(self, secret="s", signal_id="chart-1"):
        client = AsyncMock()
        client.get_contract_status.return_value = {
            "limit_order": {"take_profit": {"order_amount": 5.0}, "stop_loss": {"order_amount": -2.5}},
        }
        with patch("app.main.DerivClient", return_value=client):
            return self.client.post("/webhook/status", json={"secret": secret, "signal_id": signal_id})

    def test_pending_until_the_journal_row_exists(self):
        self.assertEqual(self._status().json(), {"status": "pending"})

    def test_executed_entry_reports_trade_and_deriv_tpsl(self):
        result, _ = self._process(_signal(signal_id="chart-1"))
        result.trade.exchange_order_id = "9"
        self.db.commit()
        body = self._status().json()
        self.assertEqual(body["status"], "executed")
        self.assertEqual((body["take_profit"], body["stop_loss"]), (5.0, 2.5))
        self.assertEqual(body["stake"], 10.0)

    def test_rejected_entry_reports_reason(self):
        self._process(_signal(signal_id="chart-1", take_profit=990.0))
        body = self._status().json()
        self.assertEqual(body["status"], "rejected")
        self.assertIn("Take profit", body["reason"])

    def test_wrong_secret_is_refused(self):
        self.assertEqual(self._status(secret="x").status_code, 403)

    def test_tpsl_dialog_gets_prices_and_contract_figures(self):
        result, _ = self._process(_signal())
        result.trade.exchange_order_id = "9"
        self.db.commit()
        client = AsyncMock()
        client.get_contract_status.return_value = {
            "contract_type": "MULTUP", "entry_spot": "7342.1", "buy_price": "100.00", "multiplier": 750,
            "commission": "1.23",
            "limit_order": {
                "take_profit": {"order_amount": 361.68, "value": "7377.7"},
                "stop_loss": {"order_amount": -72.54, "value": "7335.1"},
            },
        }
        with patch("app.main.DerivClient", return_value=client):
            body = self.client.get(f"/api/open-positions/{result.trade.id}/tpsl").json()
        self.assertEqual((body["take_profit"], body["stop_loss"]), (361.68, 72.54))
        self.assertEqual((body["take_profit_price"], body["stop_loss_price"]), (7377.7, 7335.1))
        self.assertEqual(
            (body["entry_spot"], body["stake"], body["multiplier"], body["commission"], body["direction"]),
            (7342.1, 100.0, 750.0, 1.23, "long"),
        )


if __name__ == "__main__":
    unittest.main()
