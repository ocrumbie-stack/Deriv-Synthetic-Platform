import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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


class ChartTpSlValidationTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://")
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


if __name__ == "__main__":
    unittest.main()
